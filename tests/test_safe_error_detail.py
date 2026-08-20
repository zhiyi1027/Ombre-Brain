from errors import safe_error_detail


def test_safe_error_detail_redacts_common_credentials_and_flattens_lines():
    error = RuntimeError(
        "Bearer secret-token\napi_key=visible-secret token: another-secret "
        "sk-abcdefgh12345678"
    )

    detail = safe_error_detail(error)

    assert "secret-token" not in detail
    assert "visible-secret" not in detail
    assert "another-secret" not in detail
    assert "sk-abcdefgh12345678" not in detail
    assert detail.count("[REDACTED]") == 4
    assert "\n" not in detail


def test_safe_error_detail_is_bounded_and_keeps_empty_exception_type():
    assert len(safe_error_detail(RuntimeError("x" * 500))) == 200
    assert safe_error_detail(RuntimeError()) == "RuntimeError"
