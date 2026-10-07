"""Verified cloud TLS using system trust plus packaged Mozilla roots."""
from __future__ import annotations

import ssl
import urllib.request
from functools import lru_cache

import certifi


@lru_cache(maxsize=1)
def cloud_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # Add roots; do not replace locally managed Windows trust or disable checks.
    context.load_verify_locations(cafile=certifi.where())
    return context


def cloud_urlopen(request, *, timeout=20):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    if str(url).lower().startswith('https://'):
        return urllib.request.urlopen(request, timeout=timeout, context=cloud_ssl_context())
    return urllib.request.urlopen(request, timeout=timeout)


def verify_packaged_roots() -> int:
    # Exercise the actual packaged CA file independently of Windows root stores.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certifi.where())
    count = context.cert_store_stats()['x509_ca']
    if count < 100 or context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError('Packaged TLS trust is incomplete')
    return count
