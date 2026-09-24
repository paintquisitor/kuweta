"""Optional adapter for the user's existing, OpenAI-compatible Qwen server.

Camera-free simulations never call this adapter or imply an AI analysis occurred.
"""
import base64
import json
import os
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelUnavailable(Exception):
    """Service/connectivity failure; must not exhaust a recording's retry budget."""


def configuration():
    return {"configured": bool(os.getenv("QWEN_BASE_URL")),
            "model": os.getenv("QWEN_MODEL", "qwen"), "used_in_simulation": False}


def analyze_images(images, context, *, json_output=False, task='identity', image_labels=None):
    """Analyze [(mime, bytes), ...]; caller must supply actual evidence images."""
    base = os.getenv("QWEN_BASE_URL", "").rstrip("/")
    if not base:
        raise ModelUnavailable("Brak konfiguracji połączenia z modelem")
    if not 1 <= len(images) <= 8:
        raise ValueError("Wymagane 1–8 obrazów")
    if image_labels is not None and (len(image_labels) != len(images) or
                                    any(not isinstance(label, str) for label in image_labels)):
        raise ValueError('Podpisy muszą odpowiadać obrazom')
    if task not in ('presence', 'identity'):
        raise ValueError('Obsługiwane jest tylko wykrywanie i rozpoznawanie kota')
    instructions = (
        'Sprawdź wyłącznie obecność kota w kuwecie. Opisz tylko widoczne fakty. '
        'Nie oceniaj nasady ogona, moczu ani kału. '
    ) if task == 'presence' else (
        'Porównaj tożsamość kotów z oznaczonymi obrazami wzorcowymi. '
        'Opieraj się wyłącznie na widocznej sylwetce, ogonie i sierści; nie zgaduj. '
        'Obrazy wzorcowe i obrazy do oceny to osobne grupy, nie sekwencja jednej wizyty. ')
    content = [{"type": "text", "text": instructions + 'Kontekst: ' + context}]
    for index, (mime, data) in enumerate(images):
        if mime not in ("image/jpeg", "image/png", "image/webp") or len(data) > 5_000_000:
            raise ValueError("Nieobsługiwany lub zbyt duży obraz")
        if image_labels is not None:
            content.append({'type': 'text', 'text': image_labels[index]})
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}})
    payload = json.dumps({"model": os.getenv("QWEN_MODEL", "qwen"),
                          "messages": [{"role": "user", "content": content}],
                          "temperature": 0.1, "max_tokens": 2000 if json_output else 800,
                          **({"response_format": {"type": "json_object"}} if json_output else {}),
                          "chat_template_kwargs": {"enable_thinking": False}}).encode()
    headers = {"Content-Type": "application/json"}
    if os.getenv("QWEN_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["QWEN_API_KEY"]
    request = Request(base + "/chat/completions", data=payload, headers=headers)
    try:
        with urlopen(request, timeout=120) as response:
            result = json.loads(response.read(2_000_000))
    except HTTPError as error:
        error.close()
        # Authentication/configuration and server outages are independent of the film.
        if error.code in (401, 403, 404, 408, 429) or error.code >= 500:
            raise ModelUnavailable("Model niedostępny; analiza oczekuje wznowienia") from None
        raise
    except (URLError, TimeoutError, ConnectionError, HTTPException):
        raise ModelUnavailable("Brak odpowiedzi modelu; analiza oczekuje wznowienia") from None
    # Return observations, never automatically promote them to confirmed urine.
    observation = result["choices"][0]["message"].get("content")
    if not isinstance(observation, str) or not observation.strip():
        raise ValueError("Qwen nie zwrócił opisu obrazu; analiza nie została ukończona")
    return observation
