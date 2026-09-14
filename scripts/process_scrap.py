import os
import re
import json
import time
from datetime import datetime
import requests
from google import genai
from google.genai import types

# GitHub Event 파일에서 이슈 정보 추출
event_path = os.environ.get("GITHUB_EVENT_PATH")
issue_number = os.environ.get("ISSUE_NUMBER", "1")
issue_title = os.environ.get("ISSUE_TITLE", "")
issue_body = os.environ.get("ISSUE_BODY", "")

if event_path and os.path.exists(event_path):
    try:
        with open(event_path, "r", encoding="utf-8") as f:
            event_data = json.load(f)
        issue = event_data.get("issue", {})
        issue_number = str(issue.get("number", issue_number))
        issue_title = issue.get("title", issue_title)
        issue_body = issue.get("body", issue_body) or ""
    except Exception as e:
        print(f"이벤트 데이터 로드 실패: {e}")

api_key = os.environ.get("GEMINI_API_KEY")
github_token = os.environ.get("GITHUB_TOKEN")
repo = os.environ.get("GITHUB_REPOSITORY")

os.makedirs("scraps", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("images", exist_ok=True)

# 1. 본문에서 외부 원문 링크(URL) 추출 (릴스, 블로그, 유튜브 등)
raw_urls = re.findall(r'(https?://[^\s\)\"\'>]+)', issue_body)
external_urls = [u for u in raw_urls if "github.com/user-attachments" not in u and not u.endswith(('.jpg', '.png', '.webp', '.jpeg'))]
source_url = external_urls[0] if external_urls else ""

# 2. 본문에서 이미지 주소 추출
image_markdown_urls = re.findall(r'!\[.*?\]\((https?://[^\s\)]+)\)', issue_body)
image_html_urls = re.findall(r'<img[^>]+src=[\"\'](https?://[^\'\"\s>]+)[\"\']', issue_body)
raw_attachment_urls = re.findall(r'(https?://github\.com/user-attachments/assets/[^\s\"\'\)]+)', issue_body)
all_image_urls = list(dict.fromkeys(image_markdown_urls + image_html_urls + raw_attachment_urls))

# 다중 헤더 시도를 통한 안전한 이미지 다운로드 함수
def fetch_image_bytes(url):
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if res.status_code == 200 and len(res.content) > 0:
            return res.content
    except Exception:
        pass
    if github_token:
        try:
            res = requests.get(url, headers={"Authorization": f"token {github_token}", "User-Agent": "Mozilla/5.0"}, timeout=20)
            if res.status_code == 200 and len(res.content) > 0:
                return res.content
        except Exception:
            pass
    return None

saved_images = []
for idx, img_url in enumerate(all_image_urls):
    content = fetch_image_bytes(img_url)
    if content:
        ext = "jpg"
        if ".png" in img_url.lower():
            ext = "png"
        elif ".webp" in img_url.lower():
            ext = "webp"
        
        img_filename = f"images/scrap_{issue_number}_{idx+1}.{ext}"
        with open(img_filename, "wb") as f:
            f.write(content)
        saved_images.append(img_filename)
        print(f"이미지 저장 성공: {img_filename}")

# Gemini API 클라이언트 초기화
client = genai.Client(api_key=api_key)

prompt = f"""
당신은 개인 지식 아카이빙 전문가입니다.
사용자가 수집한 정보(제목: '{issue_title}', 본문: '{issue_body}')를 분석하여 JSON 형식으로 구조화해 주세요.

규칙:
1. `summary`: 핵심 내용 2~3줄 요약 (한국어)
2. `category`: 주 카테고리 1개 (예: 반려동물, 사진/조명, 디자인, 인테리어, 테크 등)
3. `tags`: 검색용 키워드 태그 3~6개 리스트 (문자열 배열)
4. `ocr_text`: 이미지 속 텍스트가 있다면 글자 그대로 추출 (없으면 빈 문자열)
5. `user_intent`: 사용자가 이 자료를 저장한 실질적 목적 추정 1줄

반드시 순수 JSON 형식만 반환하세요.
"""

contents = [prompt]
for img_path in saved_images:
    try:
        with open(img_path, "rb") as f:
            img_bytes = f.read()
        mime = "image/png" if img_path.endswith(".png") else "image/jpeg"
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
    except Exception as e:
        print(f"이미지 첨부 실패: {e}")

# 구글 서버 과부하(503) 대응 캐스케이드
candidate_models = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"]
response = None

for model_name in candidate_models:
    for attempt in range(2):
        try:
            print(f"[{model_name}] 분석 요청 중... (시도 {attempt+1})")
            response = client.models.generate_content(
                model=model_name,
                contents=contents
            )
            if response and response.text:
                break
        except Exception as e:
            print(f"[{model_name}] 오류 발생: {e}")
            time.sleep(3)
    if response and response.text:
        break

raw_text = response.text.strip() if response and response.text else ""
analysis = {}

json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
if json_match:
    try:
        analysis = json.loads(json_match.group(0))
    except Exception:
        pass

if not analysis:
    analysis = {
        "summary": raw_text[:250] if raw_text else issue_title,
        "category": "일반",
        "tags": ["스크랩"],
        "ocr_text": "",
        "user_intent": issue_title
    }

# 마크다운 내용 조립
today = datetime.now().strftime("%Y-%m-%d")
md_filename = f"scraps/{today}-scrap-{issue_number}.md"

ocr_content = analysis.get("ocr_text", "")
ocr_section = f"### 🔍 이미지 속 텍스트 (OCR)\n{ocr_content}\n\n" if ocr_content else ""

images_section = ""
if saved_images:
    img_lines = [f"![image](../{img})" for img in saved_images]
    images_section = "### 🖼️ 캡처 이미지\n" + "\n".join(img_lines) + "\n\n"

source_link_section = f"### 🔗 원문 링크\n[{source_url}]({source_url})\n\n" if source_url else ""

tag_list_str = ", ".join(["#" + str(t) for t in analysis.get("tags", [])])
tag_yaml_str = ", ".join([f'"{t}"' for t in analysis.get("tags", [])])

md_content = f"""---
id: {issue_number}
date: {today}
title: "{issue_title}"
category: "{analysis.get('category', '일반')}"
tags: [{tag_yaml_str}]
source_url: "{source_url}"
user_intent: "{analysis.get('user_intent', '')}"
---

# {issue_title}

### 📌 핵심 요약
{analysis.get('summary', '')}

### 🏷️ 태그 & 카테고리
* **분류**: `{analysis.get('category', '일반')}`
* **태그**: {tag_list_str}

{source_link_section}### 📝 원본 내용 / 메모
{issue_body}

{ocr_section}{images_section}"""

with open(md_filename, "w", encoding="utf-8") as f:
    f.write(md_content)

# 전체 데이터 인덱스(data/scraps.json) 갱신
index_path = "data/scraps.json"
records = []
if os.path.exists(index_path):
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            records = json.load(f)
    except Exception:
        records = []

records.insert(0, {
    "id": issue_number,
    "date": today,
    "title": issue_title,
    "category": analysis.get("category", "일반"),
    "tags": analysis.get("tags", []),
    "summary": analysis.get("summary", ""),
    "source_url": source_url,
    "md_path": md_filename,
    "images": saved_images
})

with open(index_path, "w", encoding="utf-8") as f:
    json.dump(records, f, ensure_ascii=False, indent=2)

# Issue 댓글 및 이슈 닫기
if github_token and repo:
    comment_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    comment_body = f"""✅ **제미나이 자동 정리가 완료되었습니다!**
* **분류**: `{analysis.get('category', '일반')}`
* **태그**: {tag_list_str}
* **원문 링크**: {source_url if source_url else '없음'}
* **요약**: {analysis.get('summary', '')}
* **이미지 저장**: {f'{len(saved_images)}장 저장됨' if saved_images else '없음'}
* **저장 파일**: `{md_filename}`
"""
    requests.post(comment_url, headers={"Authorization": f"token {github_token}", "User-Agent": "Mozilla/5.0"}, json={"body": comment_body})
    requests.patch(f"https://api.github.com/repos/{repo}/issues/{issue_number}", headers={"Authorization": f"token {github_token}", "User-Agent": "Mozilla/5.0"}, json={"state": "closed"})

print("처리가 정상적으로 완료되었습니다.")
