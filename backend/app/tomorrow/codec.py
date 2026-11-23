"""Canonical hashes for already JSON-native dossiers without cloning their graphs."""
import hashlib
import json


def json_digest(value):
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode('utf-8'))
    return digest.hexdigest()
