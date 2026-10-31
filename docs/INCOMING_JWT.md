# Incoming Qlik JWT authentication

Implementation work is in progress. The goal is to authenticate each distinct incoming bearer JWT against the configured Qlik virtual proxy and isolate Qlik sessions, clients and user-sensitive caches by authenticated token. Cached sessions must never outlive the JWT expiration, and missing or invalid tokens must fail closed without falling back to a service account.

TLS verification remains optional through the existing QLIK_VERIFY_SSL setting. Do not use this branch in production until request isolation, concurrent access, SDK compatibility and live Qlik authorization have been verified.
