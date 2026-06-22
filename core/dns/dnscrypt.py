"""
UmbraNet — DNSCrypt v2 клиент
==============================

Поддержка протокола DNSCrypt (https://github.com/DNSCrypt/dnscrypt-protocol).
Нужен в основном для ПОЛЬЗОВАТЕЛЬСКИХ профилей: человек может указать свой
DNSCrypt-сервер (AdGuard, Quad9 и т.п.) через sdns:// штамп.

Криптография — через PyNaCl (libsodium): X25519 + XSalsa20-Poly1305 или
XChaCha20-Poly1305. Свою крипту НЕ пишем.

Как это работает (кратко):
  1) Разбираем sdns:// штамп → адрес резолвера, имя провайдера, public key.
  2) Делаем обычный (нешифрованный) DNS TXT-запрос на <provider-name> и
     получаем подписанный сертификат с короткоживущим публичным ключом
     резолвера + client-magic + версией шифрования.
  3) Проверяем подпись сертификата провайдерским ключом (Ed25519).
  4) Генерируем свою пару ключей, считаем общий ключ (X25519) и шлём
     зашифрованный запрос; ответ расшифровываем и проверяем.

Зависимость PyNaCl опциональна: если её нет — doq_available()-аналог
dnscrypt_available() вернёт False, а транспорт даст понятную ошибку и fallback.
"""

import base64
import logging
import os
import socket
import struct

from dnslib import DNSRecord, QTYPE

log = logging.getLogger("UmbraNet.DNSCrypt")

CERT_MAGIC = b"DNSC"
# Версии шифрования из сертификата (es-version):
ES_XSALSA20 = 1   # X25519-XSalsa20Poly1305
ES_XCHACHA20 = 2  # X25519-XChaCha20Poly1305


def dnscrypt_available() -> bool:
    """Доступен ли DNSCrypt (установлен ли PyNaCl)."""
    import importlib.util
    return importlib.util.find_spec("nacl") is not None


# ── sdns:// штамп ────────────────────────────────────────────────────────────
def _b64url_decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def parse_stamp(stamp: str) -> dict:
    """Разбирает sdns:// штамп DNSCrypt (тип 0x01).

    Возвращает {addr, port, provider_name, provider_pk}.
    Бросает ValueError, если штамп не DNSCrypt.
    """
    if not stamp.startswith("sdns://"):
        raise ValueError("Не sdns:// штамп")
    raw = _b64url_decode(stamp[len("sdns://"):])
    if not raw:
        raise ValueError("Пустой штамп")
    proto = raw[0]
    if proto != 0x01:
        raise ValueError(f"Штамп не DNSCrypt (тип {proto}); DNSCrypt = 0x01")

    pos = 1
    pos += 8  # props (8 байт флагов) — пропускаем

    def read_lp(buf, p):
        ln = buf[p]
        p += 1
        return buf[p:p + ln], p + ln

    addr, pos = read_lp(raw, pos)          # addr (ip:port или ip)
    pk, pos = read_lp(raw, pos)            # provider public key (32 байта)
    provider_name, pos = read_lp(raw, pos) # имя провайдера

    addr_s = addr.decode("utf-8")
    host, port = _split_host_port(addr_s, default_port=443)
    return {
        "addr": host,
        "port": port,
        "provider_name": provider_name.decode("utf-8"),
        "provider_pk": bytes(pk),
    }


def _split_host_port(addr: str, default_port=443):
    # IPv6 в скобках: [::1]:443
    if addr.startswith("["):
        host, _, rest = addr[1:].partition("]")
        port = int(rest.lstrip(":")) if rest.lstrip(":") else default_port
        return host, port
    if addr.count(":") == 1:
        host, _, p = addr.partition(":")
        return host, int(p)
    return addr, default_port


# ── сетевые помощники (обычный DNS, нешифрованный) ───────────────────────────
def _ip_family(ip):
    return socket.AF_INET6 if ":" in ip else socket.AF_INET


def _udp_query(ip, port, wire, timeout):
    fam = _ip_family(ip)
    s = socket.socket(fam, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(wire, (ip, port))
        data, _ = s.recvfrom(65535)
        return data
    finally:
        s.close()


def _tcp_query(ip, port, wire, timeout):
    fam = _ip_family(ip)
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((ip, port))
        s.sendall(struct.pack("!H", len(wire)) + wire)
        ln = struct.unpack("!H", _recv_exact(s, 2))[0]
        return _recv_exact(s, ln)
    finally:
        s.close()


def _recv_exact(s, n):
    chunks = []
    while n > 0:
        d = s.recv(n)
        if not d:
            raise ConnectionError("соединение закрыто")
        chunks.append(d)
        n -= len(d)
    return b"".join(chunks)


# ── получение и проверка сертификата резолвера ───────────────────────────────
def fetch_resolver_cert(ip, port, provider_name, provider_pk, timeout=5.0):
    """Запрашивает TXT <provider_name>, проверяет подпись и выбирает лучший
    действующий сертификат. Возвращает dict с параметрами шифрования."""
    import nacl.signing
    import nacl.exceptions
    import time as _time

    q = DNSRecord.question(provider_name, "TXT")
    wire = q.pack()
    try:
        data = _udp_query(ip, port, wire, timeout)
    except Exception:
        data = _tcp_query(ip, port, wire, timeout)

    resp = DNSRecord.parse(data)
    verify_key = nacl.signing.VerifyKey(provider_pk)

    best = None
    now = int(_time.time())
    for rr in resp.rr:
        if QTYPE.get(rr.rtype) != "TXT":
            continue
        # TXT rdata — одна или несколько строк; склеиваем сырые байты
        cert = _extract_txt_bytes(rr)
        if not cert or cert[:4] != CERT_MAGIC:
            continue
        parsed = _parse_cert(cert, verify_key)
        if parsed is None:
            continue
        if not (parsed["ts_start"] <= now <= parsed["ts_end"]):
            continue
        if best is None or parsed["serial"] > best["serial"]:
            best = parsed

    if best is None:
        raise ValueError("не найден валидный DNSCrypt-сертификат")
    return best


def _extract_txt_bytes(rr):
    """Достаёт сырые байты TXT-записи (dnslib хранит как список строк)."""
    try:
        data = rr.rdata.data  # list[bytes]
        if isinstance(data, (list, tuple)):
            return b"".join(x if isinstance(x, bytes) else bytes(x) for x in data)
        if isinstance(data, bytes):
            return data
        return bytes(data)
    except Exception:
        return None


def _parse_cert(cert, verify_key):
    """Парсит и проверяет подпись сертификата DNSCrypt.

    Формат: magic(4) es-version(2) protocol-minor(2) signature(64)
            resolver-pk(32) client-magic(8) serial(4) ts-start(4) ts-end(4)
    Подпись покрывает всё начиная с resolver-pk.
    """
    import nacl.exceptions
    if len(cert) < 4 + 2 + 2 + 64 + 32 + 8 + 4 + 4 + 4:
        return None
    es_version = struct.unpack("!H", cert[4:6])[0]
    signature = cert[8:72]
    signed = cert[72:]  # resolver-pk ... ts-end
    try:
        verify_key.verify(signed, signature)
    except nacl.exceptions.BadSignatureError:
        return None
    resolver_pk = signed[0:32]
    client_magic = signed[32:40]
    serial = struct.unpack("!I", signed[40:44])[0]
    ts_start = struct.unpack("!I", signed[44:48])[0]
    ts_end = struct.unpack("!I", signed[48:52])[0]
    if es_version not in (ES_XSALSA20, ES_XCHACHA20):
        return None
    return {
        "es_version": es_version,
        "resolver_pk": resolver_pk,
        "client_magic": client_magic,
        "serial": serial,
        "ts_start": ts_start,
        "ts_end": ts_end,
    }


# ── шифрованный запрос ───────────────────────────────────────────────────────
def _encrypt_query(cert, request, timeout):
    import nacl.bindings as b

    client_sk = os.urandom(32)
    client_pk = b.crypto_scalarmult_base(client_sk)
    shared = b.crypto_box_beforenm(cert["resolver_pk"], client_sk)

    es = cert["es_version"]
    # nonce: первые половина — client-nonce (случайная), вторая — нули
    if es == ES_XSALSA20:
        half = 12
    else:  # XChaCha20
        half = 12
    client_nonce = os.urandom(half)
    nonce = client_nonce + b"\x00" * half  # полный 24-байтный nonce

    query = request.pack()
    # padding ISO/IEC 7816-4: 0x80 + нули, до кратного 64, минимум 256
    padded = bytes(query) + b"\x80"
    target = max(256, ((len(padded) + 63) // 64) * 64)
    padded += b"\x00" * (target - len(padded))
    padded = bytes(padded)  # PyNaCl требует строго bytes, не bytearray

    if es == ES_XSALSA20:
        encrypted = b.crypto_box_afternm(padded, nonce, shared)
    else:
        encrypted = b.crypto_aead_xchacha20poly1305_ietf_encrypt(
            padded, b"", nonce, shared)

    return (cert["client_magic"] + client_pk + client_nonce + encrypted,
            shared, client_nonce, es)


def _decrypt_response(data, cert, shared, client_nonce, es):
    import nacl.bindings as b

    RESOLVER_MAGIC = b"r6fnvWj8"
    if data[:8] != RESOLVER_MAGIC:
        raise ValueError("неверный resolver magic в ответе")
    # client-nonce(12) + resolver-nonce(12)
    nonce = bytes(data[8:8 + 24])
    if nonce[:12] != client_nonce:
        raise ValueError("client-nonce не совпал")
    ciphertext = bytes(data[8 + 24:])

    if es == ES_XSALSA20:
        plain = b.crypto_box_open_afternm(ciphertext, nonce, shared)
    else:
        plain = b.crypto_aead_xchacha20poly1305_ietf_decrypt(
            ciphertext, b"", nonce, shared)

    # убираем padding (0x80 ... 0x00)
    idx = plain.rfind(b"\x80")
    if idx != -1 and all(c == 0 for c in plain[idx + 1:]):
        plain = plain[:idx]
    return DNSRecord.parse(plain)


# простой кэш сертификатов: provider_name -> (cert, fetched_at)
_cert_cache = {}
_CERT_TTL = 600  # 10 минут


def query_dnscrypt(stamp, request, timeout=5.0):
    """Главная функция: резолвит DNS-запрос через DNSCrypt по sdns:// штампу.

    Возвращает dnslib.DNSRecord либо бросает исключение.
    """
    if not dnscrypt_available():
        raise RuntimeError(
            "DNSCrypt недоступен: не установлен пакет 'pynacl' "
            "(pip install pynacl)."
        )
    import time as _time

    info = parse_stamp(stamp)
    key = info["provider_name"]
    cached = _cert_cache.get(key)
    if cached and (_time.time() - cached[1]) < _CERT_TTL:
        cert = cached[0]
    else:
        cert = fetch_resolver_cert(
            info["addr"], info["port"], info["provider_name"],
            info["provider_pk"], timeout=timeout)
        _cert_cache[key] = (cert, _time.time())

    wire, shared, client_nonce, es = _encrypt_query(cert, request, timeout)

    # UDP, при truncation — TCP
    try:
        data = _udp_query(info["addr"], info["port"], wire, timeout)
    except Exception:
        data = _tcp_query(info["addr"], info["port"], wire, timeout)

    return _decrypt_response(data, cert, shared, client_nonce, es)
