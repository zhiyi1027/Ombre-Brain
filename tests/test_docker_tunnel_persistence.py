from pathlib import Path

import yaml

from web import tunnel


ROOT = Path(__file__).resolve().parents[1]
EDGE_DEFAULT = (
    "${TUNNEL_EDGE-region1.v2.argotunnel.com:7844,"
    "region2.v2.argotunnel.com:7844}"
)
TRUSTED_PROXY_DEFAULT = (
    "${OMBRE_TRUSTED_PROXY_CIDRS:-127.0.0.0/8,::1/128}"
)


def _compose(name: str) -> dict:
    with (ROOT / "deploy" / name).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _environment(service: dict) -> dict[str, str]:
    raw = service["environment"]
    if isinstance(raw, dict):
        return raw
    result = {}
    for entry in raw:
        key, _, value = entry.partition("=")
        result[key] = value
    return result


def test_source_compose_has_dns_fallback_and_persistent_bind():
    service = _compose("docker-compose.yml")["services"]["ombre-brain"]
    environment = _environment(service)

    assert environment["TUNNEL_EDGE"] == EDGE_DEFAULT
    assert environment["TUNNEL_TRANSPORT_PROTOCOL"] == "${TUNNEL_TRANSPORT_PROTOCOL:-http2}"
    assert environment["OMBRE_TRUSTED_PROXY_CIDRS"] == TRUSTED_PROXY_DEFAULT
    assert environment["OMBRE_HOST_VAULT_DIR"] == "${OMBRE_HOST_VAULT_DIR:-}"
    assert service["volumes"] == [
        {"type": "bind", "source": "${OMBRE_HOST_VAULT_DIR:-../buckets}", "target": "/app/buckets"}
    ]


def test_local_ollama_remains_opt_in():
    ollama = _compose("docker-compose.yml")["services"]["ombre-ollama"]
    assert ollama["profiles"] == ["local"]
    assert ollama["volumes"] == ["ollama-data:/root/.ollama"]


def test_tunnel_token_config_lives_inside_persistent_buckets(monkeypatch, tmp_path):
    monkeypatch.setattr(tunnel.sh, "config", {"buckets_dir": str(tmp_path)})
    payload = {"token": "test-token", "auto_start": True}

    tunnel._save_tunnel_config(payload)

    config_path = tmp_path / ".tunnel_config.json"
    assert config_path.is_file()
    assert tunnel._load_tunnel_config() == payload


def test_env_example_uses_current_container_mount_and_tunnel_endpoints():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "/app/buckets" in text
    assert "region1.v2.argotunnel.com:7844" in text
    assert "region2.v2.argotunnel.com:7844" in text
    assert "TUNNEL_TRANSPORT_PROTOCOL=http2" in text
    assert "OMBRE_TRUSTED_PROXY_CIDRS" in text
