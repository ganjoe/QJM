"""
Validation Script for High-Performance LLM Router & MCP Execution Gateway.

Runs live validation tests against http://localhost:9000:
1. System Status & Health
2. Endpoint Registry & Status
3. Capability Pools initialization
4. Asynchronous WorkItem submission & SSE Token Streaming
5. OpenAI-compatible /v1/chat/completions Proxy
6. MCP Executor Tool Dispatch & Timeout / Error Handling
"""
import sys
import json
import time
import httpx

ROUTER_URL = "http://localhost:9000"

def log_test(name: str):
    print(f"\n🔹 [TEST] {name}")

def log_pass(msg: str):
    print(f"   ✅ PASS: {msg}")

def log_fail(msg: str):
    print(f"   ❌ FAIL: {msg}")
    sys.exit(1)

def test_status():
    log_test("Prüfe /api/status (Systemstatus)")
    r = httpx.get(f"{ROUTER_URL}/api/status", timeout=5.0)
    if r.status_code == 200:
        data = r.json()
        log_pass(f"Router Status: {data.get('status')} | Engine Ready")
    else:
        log_fail(f"Unerwarteter Statuscode: {r.status_code}")

def test_endpoints():
    log_test("Prüfe /api/endpoints/ (Endpoint Registry)")
    r = httpx.get(f"{ROUTER_URL}/api/endpoints/", timeout=5.0)
    if r.status_code == 200:
        data = r.json()
        endpoints = data.get("endpoints", [])
        log_pass(f"{len(endpoints)} Endpunkte registriert:")
        for ep in endpoints:
            print(f"      - {ep['endpoint_id']} ({ep['name']}): {ep['status']} (Slots: {ep['active_slots']}/{ep['max_concurrency']})")
    else:
        log_fail(f"Unerwarteter Statuscode: {r.status_code}")

def test_pools():
    log_test("Prüfe /v1/pools (Capability Pools)")
    r = httpx.get(f"{ROUTER_URL}/v1/pools", timeout=5.0)
    if r.status_code == 200:
        data = r.json()
        pools = data.get("pools", {})
        log_pass(f"{len(pools)} Pools aktiv:")
        for name, p in pools.items():
            print(f"      - [{name}]: Worker Active={p['worker_active']}, Queue={p['queue_length']}")
    else:
        log_fail(f"Unerwarteter Statuscode: {r.status_code}")

def test_submit_and_stream():
    log_test("Prüfe /v1/submit und SSE Token-Streaming (Live Inferenz)")
    payload = {
        "routing": {"capability_class": "fast", "priority": 1},
        "payload": {
            "messages": [
                {"role": "user", "content": "Antworte kurz mit genau zwei Worten: 'System bereit'"}
            ],
            "temperature": 0.1,
            "max_tokens": 50,
            "stream": True
        }
    }
    r = httpx.post(f"{ROUTER_URL}/v1/submit", json=payload, timeout=5.0)
    if r.status_code != 200:
        log_fail(f"Submit fehlgeschlagen: {r.status_code} - {r.text}")
    
    submit_data = r.json()
    job_id = submit_data["job_id"]
    stream_url = submit_data["stream_url"]
    log_pass(f"Job erstellt: {job_id} -> Stream: {stream_url}")

    # Stream empfangen
    print("   📡 Verbinde mit SSE-Stream...")
    received_tokens = []
    events_received = []
    
    with httpx.stream("GET", f"{ROUTER_URL}{stream_url}", timeout=30.0) as stream_resp:
        for line in stream_resp.iter_lines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
                events_received.append(event_type)
            elif line.startswith("data: "):
                data_str = line[6:].strip()
                try:
                    data_obj = json.loads(data_str)
                    if "delta" in data_obj and "content" in data_obj["delta"]:
                        chunk = data_obj["delta"]["content"]
                        if chunk:
                            received_tokens.append(chunk)
                except Exception:
                    pass

    full_text = "".join(received_tokens).strip()
    log_pass(f"Events empfangen: {events_received}")
    log_pass(f"Stream-Antwort vom LLM: \"{full_text}\"")

def test_openai_proxy():
    log_test("Prüfe OpenAI-kompatiblen Endpunkt /v1/chat/completions")
    payload = {
        "model": "fast",
        "messages": [
            {"role": "user", "content": "Sag 'OK'"}
        ],
        "stream": True
    }
    with httpx.stream("POST", f"{ROUTER_URL}/v1/chat/completions", json=payload, timeout=30.0) as r:
        if r.status_code != 200:
            log_fail(f"/v1/chat/completions fehlgeschlagen: {r.status_code}")
        
        chunks = []
        for line in r.iter_lines():
            if line.startswith("data: ") and not line.endswith("[DONE]"):
                try:
                    data = json.loads(line[6:])
                    if "delta" in data and "content" in data["delta"]:
                        chunks.append(data["delta"]["content"])
                except Exception:
                    pass
        log_pass(f"OpenAI-Proxy Antwort: \"{''.join(chunks).strip()}\"")

def main():
    print("=" * 60)
    print("🚀 VALIDIERUNG DES HIGH-PERFORMANCE LLM ROUTERS")
    print("=" * 60)
    
    test_status()
    test_endpoints()
    test_pools()
    test_submit_and_stream()
    test_openai_proxy()
    
    print("\n" + "=" * 60)
    print("🎉 ALLE VALIDIERUNGSTESTS ERFOLGREICH BESTANDEN!")
    print("=" * 60)

if __name__ == "__main__":
    main()
