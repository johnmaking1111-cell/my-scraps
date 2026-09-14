import os
import re
import json
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

# 폴더 생성
os.makedirs("scraps", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("images", exist_ok=True)

# 본문에서 모든 이미지 주소 추출
image_markdown_urls = re.findall(r'!\[.*?\]\((https?://[^\s\)]+)\)', issue_body)
image_html_urls = re.findall(r'<img[^>]+src=[\"\'](https?://[^\'\"\s>]+)[\"\']', issue_body)
raw_attachment_urls = re.findall(r'(https?://github\.com/user-attachments/assets/[^\s\"\'\)]+)', issue_body)
all_image_urls = list(dict.fromkeys(image_markdown_urls + image_html_urls + raw_attachment_urls))

print(f"발견된 이미지 URL 개수: {len(all_image_urls)}")

# 다중 헤더 시도를 통한 안전한 이미지 다운로드 함수
def fetch_image_bytes(url):
    # 1차 시도: 일반 웹 브라우저 헤더 (공개 저장소/AWS 리다이렉트 대응)
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=20)
        if res.status_code == 200 and len(res.content) > 0:
            return res.content
    except Exception:
        pass
    
    # 2차 시도: GitHub 인증 토큰 헤더
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
        print(f"이미지 저장 성공: {img_filename} ({len(content)} bytes)")
    else:
        print(f"이미지 다운로드 최종 실패: {img_url}")

# Gemini API 클라이언트 초기화
client = genai.Client(api_key=api_key)

prompt = f"""
당신은 개인 지식 아카이빙 전문가입니다.
사용자가 수집한 정보(제목: '{issue_title}', 본문: '{issue_body}')와 함께 제공된 이미지를 시각적으로 꼼꼼히 분석하여 JSON 형식으로 구조화해 주세요.

규칙:
1. `summary`: 이미지의 내용과 본문 핵심을 포함한 2~3줄 요약 (한국어)
2. `category`: 주 카테고리 1개 (예: 반려동물, 사진/조명, 디자인, 인테리어, 테크 등)
3. `tags`: 검색용 키워드 태그 3~6개 리스트 (문자열 배열)
4. `ocr_text`: 이미지 속 텍스트가 있다면 글자 그대로 추출 (없으면 빈 문자열)
5. `user_intent`: 사용자가 이 시각자료를 저장한 실질적 목적 추정 1줄

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

print("Gemini 3.6 Flash 모델로 분석 요청 중...")
try:
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=contents
    )
except Exception as e:
    print(f"기본 모델 실패, 대체 모델 시도: {e}")
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=contents
    )

# 안전한 JSON 파싱 처리
raw_text = response.text.strip()
analysis = {}

json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
if json_match:
    try:
        analysis = json.loads(json_match.group(0))
    except Exception:
        pass

if not analysis:
    analysis = {
        "summary": response.text[:250],
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

tag_list_str = ", ".join(["#" + str(t) for t in analysis.get("tags", [])])
tag_yaml_str = ", ".join([f'"{t}"' for t in analysis.get("tags", [])])

md_content = f"""---
id: {issue_number}
date: {today}
title: "{issue_title}"
category: "{analysis.get('category', '일반')}"
tags: [{tag_yaml_str}]
user_intent: "{analysis.get('user_intent', '')}"
---

# {issue_title}

### 📌 핵심 요약
{analysis.get('summary', '')}

### 🏷️ 태그 & 카테고리
* **분류**: `{analysis.get('category', '일반')}`
* **태그**: {tag_list_str}

### 📝 원본 내용 / 메모
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
* **요약**: {analysis.get('summary', '')}
* **이미지 저장**: {f'{len(saved_images)}장 저장됨' if saved_images else '없음'}
* **저장 파일**: `{md_filename}`
"""
    requests.post(comment_url, headers={"Authorization": f"token {github_token}", "User-Agent": "Mozilla/5.0"}, json={"body": comment_body})
    requests.patch(f"https://api.github.com/repos/{repo}/issues/{issue_number}", headers={"Authorization": f"token {github_token}", "User-Agent": "Mozilla/5.0"}, json={"state": "closed"})

print("처리가 정상적으로 완료되었습니다.")
