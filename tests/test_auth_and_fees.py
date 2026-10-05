import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshi_bot.fees import fee_per_contract_cents, taker_fee_cents
from kalshi_bot.kalshi_client import KalshiClient, KalshiError, load_private_key, sign


@pytest.fixture(scope="module")
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


def test_signature_verifies(rsa_pem):
    key = load_private_key(rsa_pem)
    sig = sign(key, "1700000000000", "GET", "/trade-api/v2/portfolio/balance")
    key.public_key().verify(
        base64.b64decode(sig), b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_non_rsa_key_rejected():
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    with pytest.raises(KalshiError, match="RSA"):
        load_private_key(pem)


def test_headers_sign_path_without_query(rsa_pem, monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        content = b"{}"
        text = "{}"
        def json(self): return {}

    class FakeSession:
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            captured.update(method=method, url=url, params=params, headers=headers)
            return FakeResp()

    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", "key-id", rsa_pem, session=FakeSession())
    client.request("GET", "/markets", params={"limit": 5})
    h = captured["headers"]
    assert h["KALSHI-ACCESS-KEY"] == "key-id"
    assert captured["url"] == "https://demo-api.kalshi.co/trade-api/v2/markets"
    client.private_key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_unauthenticated_portfolio_call_fails():
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2")
    with pytest.raises(KalshiError):
        client.get_balance()


def test_place_order_validation(rsa_pem):
    client = KalshiClient("https://x/trade-api/v2", "id", rsa_pem)
    with pytest.raises(ValueError):
        client.place_order("T", "maybe", 1, 50)
    with pytest.raises(ValueError):
        client.place_order("T", "yes", 1, 100)
    with pytest.raises(ValueError):
        client.place_order("T", "yes", 0, 50)


@pytest.mark.parametrize("count,price,expected", [
    (1, 50, 2),      # 0.07*0.25 = 1.75c -> 2c
    (10, 50, 18),    # 17.5c -> 18c
    (1, 10, 1),      # 0.63c -> 1c
    (100, 1, 7),     # 6.93c -> 7c
    (10, 99, 1),     # 0.693c -> 1c
])
def test_taker_fee(count, price, expected):
    assert taker_fee_cents(count, price) == expected


def test_fee_per_contract_peaks_at_50():
    assert fee_per_contract_cents(50) == pytest.approx(1.75)
    assert fee_per_contract_cents(10) < fee_per_contract_cents(50)
