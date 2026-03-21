"""Core quiz generation logic — no FastAPI dependencies here."""
import json
import re
from typing import Any, Dict, List

from fastapi import HTTPException

from core.llm import get_llm_client
from tools.quiz_generator.models import QuizQuestion, QuizRequest, QuizResponse

_VALID_ESCAPES = set('"\\' + "/bfnrt")


def _sanitize(content: str) -> str:
    """Strip BOM and control characters that break JSON parsing."""
    content = content.lstrip("\ufeff")
    content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", content)
    return content


def _fix_json_escapes(content: str) -> str:
    """Replace invalid JSON escape sequences with their literal character."""
    result = []
    i = 0
    while i < len(content):
        ch = content[i]
        if ch == "\\" and i + 1 < len(content):
            nxt = content[i + 1]
            if nxt in _VALID_ESCAPES or nxt == "u":
                result.append(ch)
                result.append(nxt)
                i += 2
                continue
            # Invalid escape — emit the literal character
            result.append(nxt)
            i += 2
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _build_prompt(payload: QuizRequest) -> str:
    topics_text = "\n".join(f"- {t}" for t in payload.topics)
    sources_text = "\n\n".join(
        f"{s.title or f'Source {i+1}'}:\n{s.content}"
        for i, s in enumerate(payload.sources)
    )

    return f"""You are an expert quiz generator.

Generate a quiz strictly based on the provided topics and sources.

Requirements:
- Number of questions: {payload.num_questions}
- Difficulty: {payload.difficulty} (easy, medium, hard, or mixed)
- Prefer multiple-choice questions with 4 options.
- Each question must have:
  - question: the question text
  - options: array of 4 answer choices
  - answer: the correct answer (must match one of the options exactly)
  - explanation: short explanation referencing the source material

Return ONLY valid JSON:
{{
  "questions": [
    {{
      "question": "string",
      "options": ["string", "string", "string", "string"],
      "answer": "string",
      "explanation": "string"
    }}
  ]
}}

Topics:
{topics_text}

Sources:
{sources_text}""".strip()


def _parse_response(content: str) -> Dict[str, Any]:
    content = _sanitize(content)
    content = _fix_json_escapes(content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(content[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise


def generate(payload: QuizRequest) -> QuizResponse:
    client, model = get_llm_client()
    prompt = _build_prompt(payload)

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a quiz generator. Return clean JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM API error: {exc}") from exc

    raw = completion.choices[0].message.content or ""

    try:
        data = _parse_response(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to parse model output: {exc}") from exc

    questions_data = data.get("questions")
    if not isinstance(questions_data, list):
        raise HTTPException(status_code=502, detail="Model response missing 'questions'.")

    questions: List[QuizQuestion] = []
    for item in questions_data:
        try:
            questions.append(QuizQuestion(**item))
        except Exception:
            continue

    if not questions:
        raise HTTPException(status_code=502, detail="No valid questions generated.")

    return QuizResponse(questions=questions)
