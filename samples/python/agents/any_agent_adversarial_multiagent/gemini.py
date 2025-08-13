import json
import os
import sys
import urllib.request


DEFAULT_MODEL_ID = "gemini-2.0-flash-lite"
API_ENDPOINT = (
	"https://generativelanguage.googleapis.com/v1beta/models/"
	"{model}:generateContent?key={api_key}"
)


def call_gemini(api_key: str, model: str, prompt: str, timeout: float = 20.0) -> str:
	url = API_ENDPOINT.format(model=model, api_key=api_key)
	payload = {
		"contents": [
			{
				"role": "user",
				"parts": [
					{"text": prompt},
				],
			}
		]
	}
	data = json.dumps(payload).encode("utf-8")
	req = urllib.request.Request(
		url,
		data=data,
		headers={"Content-Type": "application/json"},
		method="POST",
	)
	with urllib.request.urlopen(req, timeout=timeout) as resp:
		body = resp.read()
	obj = json.loads(body.decode("utf-8"))
	# Extract first candidate text if available
	try:
		parts = obj["candidates"][0]["content"]["parts"]
		texts = [p.get("text", "") for p in parts]
		return "".join(texts).strip()
	except Exception:
		return json.dumps(obj, ensure_ascii=False)


def main() -> None:
	api_key = os.environ.get("GEMINI_API_KEY")
	if not api_key:
		print("GEMINI_API_KEY is not set", file=sys.stderr)
		sys.exit(1)

	model = os.environ.get("GEMINI_MODEL_ID", DEFAULT_MODEL_ID)
	text = call_gemini(api_key=api_key, model=model, prompt="ping")
	print(text or "<empty>")


if __name__ == "__main__":
	main()

