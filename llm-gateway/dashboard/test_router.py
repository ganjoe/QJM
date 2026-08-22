"""
Validation Script for Switchyard LLM Gateway & Orchestrator Control Plane.

Runs validation tests against:
1. Orchestrator Status (http://localhost:9000/api/status)
2. Switchyard Routes & Config (http://localhost:9000/api/routing/)
3. Backends Status (http://localhost:9000/api/backends/)
4. Switchyard Router Endpoints (http://localhost:4000/v1/models)
"""
import sys
import json
import httpx

ORCHESTRATOR_URL = "http://localhost:9000"
SWITCHYARD_URL = "http://localhost:4000"


def log_test(name: str):
    print(f"\n🔹 [TEST] {name}")


def log_pass(msg: str):
    print(f"   ✅ PASS: {msg}")


def log_fail(msg: str):
    print(f"   ❌ FAIL: {msg}")


def test_orchestrator_status():
    log_test("Prüfe Orchestrator Status (/api/status)")
    try:
        r = httpx.get(f"{ORCHESTRATOR_URL}/api/status", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            log_pass(f"Status: {data.get('status')} | Service: {data.get('service')}")
            log_pass(f"Router Info: {data.get('router')}")
        else:
            log_fail(f"Unerwarteter Statuscode: {r.status_code}")
    except Exception as e:
        log_fail(f"Orchestrator nicht erreichbar: {e}")


def test_routing_table():
    log_test("Prüfe Switchyard Routing Table (/api/routing/)")
    try:
        r = httpx.get(f"{ORCHESTRATOR_URL}/api/routing/", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            routes = data.get("routes", [])
            log_pass(f"{len(routes)} Routen in Switchyard konfiguriert:")
            for route in routes:
                print(f"      - [{route['tier']}] -> {route['target']} ({route['api_base']})")
        else:
            log_fail(f"Unerwarteter Statuscode: {r.status_code}")
    except Exception as e:
        log_fail(f"Routing API nicht erreichbar: {e}")


def test_backends_status():
    log_test("Prüfe Backends Status (/api/backends/)")
    try:
        r = httpx.get(f"{ORCHESTRATOR_URL}/api/backends/", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            backends = data.get("backends", [])
            log_pass(f"{len(backends)} Backends überwacht:")
            for b in backends:
                print(f"      - {b['name']} ({b['type']}): Healthy={b.get('healthy')}")
        else:
            log_fail(f"Unerwarteter Statuscode: {r.status_code}")
    except Exception as e:
        log_fail(f"Backends API nicht erreichbar: {e}")


def test_switchyard_direct():
    log_test("Prüfe Switchyard Router direkt (http://localhost:4000/v1/models)")
    try:
        r = httpx.get(f"{SWITCHYARD_URL}/v1/models", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            models = data.get("data", [])
            log_pass(f"Switchyard online! Verfügbare Modelle: {[m.get('id') for m in models]}")
        else:
            log_fail(f"Switchyard liefert Statuscode: {r.status_code}")
    except Exception as e:
        print(f"   ⚠️ Switchyard direkt nicht erreichbar (Container läuft evtl. noch nicht): {e}")


def main():
    print("=" * 60)
    print("🚀 VALIDIERUNG: SWITCHYARD & ORCHESTRATOR")
    print("=" * 60)

    test_orchestrator_status()
    test_routing_table()
    test_backends_status()
    test_switchyard_direct()

    print("\n" + "=" * 60)
    print("🎉 VALIDIERUNG DURCHGEFÜHRT")
    print("=" * 60)


if __name__ == "__main__":
    main()
