"""LLM-as-judge grader: uses Claude to evaluate response quality."""

import logging
import subprocess
import json

logger = logging.getLogger("shadow-ai-evals")

JUDGE_PROMPT = """You are an expert evaluator for an AI Slack bot called shadow.ai.
Your job is to grade the bot's response on multiple dimensions.

## Context
The bot received this message in a Slack channel:
{input_text}

The bot responded with:
{response}

{extra_context}

## Grade each dimension (1-5 scale):

1. **Accuracy** — Is the response factually correct? Does it verify with tools before answering?
   1=wrong/hallucinated, 3=mostly correct, 5=perfectly accurate

2. **Completeness** — Does it fully answer the question? Missing important details?
   1=barely answers, 3=adequate, 5=comprehensive

3. **Conciseness** — Is it appropriately brief? No unnecessary preamble or filler?
   1=very verbose/padded, 3=okay, 5=perfectly concise

4. **Formatting** — Does it use Slack formatting correctly? Bold, code blocks, bullets?
   1=broken formatting, 3=okay, 5=clean Slack-native formatting

5. **Helpfulness** — Would the user find this response useful and actionable?
   1=useless, 3=somewhat helpful, 5=extremely helpful

## Output ONLY a JSON object:
{{"accuracy": N, "completeness": N, "conciseness": N, "formatting": N, "helpfulness": N, "overall": N, "feedback": "one sentence summary"}}

The "overall" score is your holistic assessment (1-5). Be honest and strict."""


def grade_quality_with_llm(input_text: str, response: str, extra_context: str = "") -> dict:
    """Use Claude CLI to grade response quality.

    Returns dict with scores and feedback, or None if grading fails.
    """
    prompt = JUDGE_PROMPT.format(
        input_text=input_text,
        response=response[:3000],  # Cap to avoid huge prompts
        extra_context=extra_context,
    )

    try:
        result = subprocess.run(
            ["claude", "--print", "--model", "haiku", "-p", prompt],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()

        # Extract JSON from response
        start = output.find("{")
        end = output.rfind("}") + 1
        if start >= 0 and end > start:
            scores = json.loads(output[start:end])
            logger.info(f"[LLM-JUDGE] Scores: {scores}")
            return {
                "scores": scores,
                "pass": scores.get("overall", 0) >= 3,
                "detail": f"Overall: {scores.get('overall', '?')}/5 — {scores.get('feedback', '')}",
            }
    except subprocess.TimeoutExpired:
        logger.warning("[LLM-JUDGE] Timed out")
    except json.JSONDecodeError as e:
        logger.warning(f"[LLM-JUDGE] Failed to parse JSON: {e}")
    except Exception as e:
        logger.warning(f"[LLM-JUDGE] Error: {e}")

    return None
