import os
import re
import json
from datetime import datetime
import requests
from google import genai
from google.genai import types

# 환경 변수 로드
api_key = os.environ.get("GEMINI_API_KEY")
github_token = os.environ.get("GITHUB_TOKEN")
issue_number = os.environ.get("ISSUE_NUMBER")
issue_title = os.environ.get("ISSUE_TITLE", "")
issue_body = os.environ.get("ISSUE_BODY", "")
repo = os.environ.get("GITHUB_REPOSITORY")

# 폴더 생성
os.makedirs("scraps", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("images", exist_ok=True)

# URL 및 이미지 주소 추출
urls = re.findall(r'(https?://[^\s\)]+)', issue_body)
image_markdown_urls = re.findall(r'!\[.*?\]\((https?://.*?)\)', issue_body)

# 이미지 다운로드 처리
saved_images = []
headers = {"Authorization": f"token {github_token}"} if github_token else {}

for idx, img_url in enumerate(image_markdown_urls):
    try:
        res = requests.get(img_url, headers=headers, timeout=15)
        if res.status_code == 200:
            ext = img_url.split("?")[0].split(".")[-1]
            if len(ext) > 4 or "/" in ext:
                ext = "jpg"
            img_filename = f"images/scrap_{issue_number}_{idx+1}.{ext}"
            with open(img_filename, "wb") as f:
                f.write(res.content)
            saved_images.append(img_filename)
    except Exception as e:
        print(f"이미지 다운로드 실패 ({img_url}): {e}")

# Gemini API 클라이언트 초기화
client = genai.Client(api_key=api_key)

prompt = f"""
당신은 개인 지식 아카이빙 전문가입니다.
사용자가 수집한 정보(제목: '{issue_title}', 내용: '{issue_body}')를 분석하여 JSON 형식으로 구조화해 주세요.

다음 규칙을 반드시 지켜주세요:
1. `summary`: 핵심 내용 2~3줄 요약 (한국어)
2. `category`: 주 카테고리 1개 (예: 사진/조명, 디자인, 테크/코딩, 비즈니스, 라이프스타일 등 적절한 카테고리)
3. `tags`: 검색용 키워드 태그 3~6개 리스트
4. `ocr_text`: 이미지 속 텍스트가 있다면 추출 (없으면 빈 문자열)
5. `user_intent`: 사용자가 이 정보를 왜 저장했는지 추정되는 목적 1줄

응답은 반드시 마크다운 코드블록(```json ... ```)을 포함한 JSON 형식이어야 합니다.
"""

contents = [prompt]
for img_path in saved_images:
    with open(img_path, "rb") as f:
        img_bytes = f.read()
    contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))

# 제미나이 분석 실행
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=contents
)

# JSON 파싱
clean_text = response.text.replace("```json", "").replace("```", "").strip()
try:
    analysis = json.loads(clean_text)
except Exception:
    analysis = {
        "summary": response.text[:200],
        "category": "기타",
        "tags": ["스크랩"],
        "ocr_text": "",
        "user_intent": issue_title
    }

# 마크다운 파일 생성
today = datetime.now().strftime("%Y-%m-%d")
md_filename = f"scraps/{today}-scrap-{issue_number}.md"

img_md = "\n".join([f"![image](../{img})" for img in saved_images])
tags_str = ", ".join([f'"{t}"' for t in analysis.get("tags", [])])

md_content = f"""---
id: {issue_number}
date: {today}
title: "{issue_title}"
category: "{analysis.get('category', '기타')}"
tags: [{tags_str}]
user_intent: "{analysis.get('user_intent', '')}"
---

# {issue_title}

### 📌 핵심 요약
{analysis.get('summary', '')}

### 🏷️ 태그 & 카테고리
* **분류**: `{analysis.get('category', '기타')}`
* **태그**: {', '.join(['#' + t for t in analysis.get('tags', [])])}

### 📝 원본 내용 / 메모
{issue_body}

{f"### 🔍 이미지 속 텍스트 (OCR)\n{analysis.get('ocr_text')}\n" if analysis.get('ocr_text') else ""}
{f"### 🖼️ 캡처 이미지\n{img_md}\n" if img_md else ""}
"""

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
    "category": analysis.get("category", "기타"),
    "tags": analysis.get("tags", []),
    "summary": analysis.get("summary", ""),
    "md_path": md_filename,
    "images": saved_images
})

with open(index_path, "w", encoding="utf-8") as f:
    json.dump(records, f, ensure_ascii=False, indent=2)

# Issue에 분석 완료 댓글 달고 이슈 닫기
if github_token and repo:
    comment_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    comment_body = f"""✅ **제미나이 자동 정리가 완료되었습니다!**
* **분류**: `{analysis.get('category', '기타')}`
* **요약**: {analysis.get('summary', '')}
* **저장 파일**: `{md_filename}`
"""
    requests.post(comment_url, headers=headers, json={"body": comment_body})
    # 이슈 종료
    requests.patch(f"https://api.github.com/repos/{repo}/issues/{issue_number}", headers=headers, json={"state": "closed"})
