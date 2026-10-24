#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import aiohttp

BASE_URL = os.getenv("CLAUDE_GW_URL", "http://127.0.0.1:8001").rstrip("/")
API_KEY = os.getenv("CLAUDE_GW_API_KEY", "")


async def run_tests() -> bool:
    print(f"Connecting to Claude Gateway at {BASE_URL}...")
    headers = {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    all_passed = True

    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        # 1. Health check
        print("\n--- 1. Testing GET /health ---")
        try:
            async with session.get(f"{BASE_URL}/health") as resp:
                print("Status:", resp.status)
                data = await resp.json()
                print("Response:", json.dumps(data, indent=2))
                assert resp.status == 200, f"Expected 200, got {resp.status}"
                assert data.get("status") == "ok", f"Expected status 'ok', got {data.get('status')}"
                print("✅ [PASS] Health check passed")
        except Exception as e:
            print(f"❌ [FAIL] Health check failed: {e}")
            all_passed = False

        # 2. Models list
        print("\n--- 2. Testing GET /v1/models ---")
        try:
            async with session.get(f"{BASE_URL}/v1/models") as resp:
                print("Status:", resp.status)
                data = await resp.json()
                models = [m["id"] for m in data.get("data", [])]
                print(f"Found {len(models)} model(s): {models[:8]}")
                assert resp.status == 200, f"Expected 200, got {resp.status}"
                assert len(models) > 0, "No models returned by /v1/models"
                assert "claude-haiku-5-5" in models or "claude-sonnet-5-5" in models, (
                    f"Expected Claude models in list, got {models}"
                )
                print("✅ [PASS] Models endpoint passed")
        except Exception as e:
            print(f"❌ [FAIL] Models endpoint failed: {e}")
            all_passed = False

        # 3. Non-streaming Chat Completion
        print("\n--- 3. Testing POST /v1/chat/completions (stream=False) ---")
        try:
            payload = {
                "model": "claude-haiku-5-5",
                "messages": [
                    {"role": "user", "content": "Reply with exactly: PONG"}
                ],
                "stream": False,
            }
            async with session.post(f"{BASE_URL}/v1/chat/completions", json=payload) as resp:
                print("Status:", resp.status)
                res_json = await resp.json()
                print("Result:", json.dumps(res_json, indent=2))
                assert resp.status == 200, f"Expected 200, got {resp.status}: {res_json}"
                choices = res_json.get("choices", [])
                assert len(choices) > 0, "No choices returned in chat completion"
                reply = choices[0].get("message", {}).get("content", "").strip()
                print(f"Assistant Reply: '{reply}'")
                assert "PONG" in reply.upper(), f"Expected 'PONG' in reply, got '{reply}'"
                print("✅ [PASS] Non-streaming chat completion passed")
        except Exception as e:
            print(f"❌ [FAIL] Non-streaming completion failed: {e}")
            all_passed = False

        # 4. Streaming Chat Completion (SSE)
        print("\n--- 4. Testing POST /v1/chat/completions (stream=True) ---")
        try:
            stream_payload = {
                "model": "claude-haiku-5-5",
                "messages": [
                    {"role": "user", "content": "Count from 1 to 5 separated by spaces."}
                ],
                "stream": True,
            }
            async with session.post(f"{BASE_URL}/v1/chat/completions", json=stream_payload) as resp:
                print("Status:", resp.status)
                assert resp.status == 200, f"Expected 200, got {resp.status}"
                print("Streaming tokens: ", end="", flush=True)

                stream_complete = False
                tokens_received = []

                async for line in resp.content:
                    decoded = line.decode("utf-8").strip()
                    if not decoded or not decoded.startswith("data: "):
                        continue
                    raw_data = decoded[6:]
                    if raw_data == "[DONE]":
                        stream_complete = True
                        print("\n[Stream Complete]")
                        break

                    try:
                        chunk = json.loads(raw_data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        reasoning = delta.get("reasoning_content", "")
                        if content:
                            tokens_received.append(content)
                            sys.stdout.write(content)
                            sys.stdout.flush()
                        elif reasoning:
                            tokens_received.append(reasoning)
                            sys.stdout.write(f"[{reasoning}]")
                            sys.stdout.flush()
                    except Exception as parse_err:
                        print(f"\nWarning: could not parse stream chunk: {parse_err}")

                assert stream_complete, "Stream ended without [DONE] signal"
                assert len(tokens_received) > 0, "No tokens received from stream"
                full_stream_text = "".join(tokens_received)
                print(f"Full streamed text: '{full_stream_text.strip()}'")
                print("✅ [PASS] Streaming chat completion passed")
        except Exception as e:
            print(f"❌ [FAIL] Streaming completion failed: {e}")
            all_passed = False

    if all_passed:
        print("\n🎉 ALL TESTS PASSED SUCCESSFULLY!")
        return True
    else:
        print("\n❌ SOME TESTS FAILED!")
        return False


def main():
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
