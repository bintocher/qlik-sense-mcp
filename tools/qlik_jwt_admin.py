"""
Admin CLI for qlik-sense-mcp JWT authentication.

Two subcommands:

    init-keys    Generate an RSA keypair and self-signed X.509 certificate
                 used by a Qlik Sense Enterprise JWT virtual proxy.

    issue-token  Sign a long-lived JWT for a single analyst, ready to paste
                 into their mcp.json as QLIK_JWT_TOKEN.

Typical workflow for the admin (run ONCE on a machine only the admin controls):

    # 1. Generate signing keys. Keep jwt_private.pem safe forever.
    python tools/qlik_jwt_admin.py init-keys --out ./jwt_keys

    # 2. Open Qlik QMC -> Configure system -> Virtual proxies -> Create new,
    #    paste ./jwt_keys/jwt_cert.pem into the "JWT certificate" field,
    #    set "JWT attribute for user ID" = userId,
    #    set "JWT attribute for user directory" = userDirectory,
    #    finish the rest of the VP wizard (prefix, session cookie, host allow list,
    #    link to Central proxy). See docs/AUTH_JWT.md for the exact values.

    # 3. For each analyst, issue a token signed with their domain identity.
    python tools/qlik_jwt_admin.py issue-token \
        --key ./jwt_keys/jwt_private.pem \
        --user-id ivanov \
        --user-directory COMPANY

    # 4. Give the analyst the printed token + the Qlik URL + the virtual
    #    proxy prefix. They put two env vars in their Cursor mcp.json and
    #    the MCP server works.

The private key NEVER leaves the admin's machine. Analysts only ever receive
a signed JWT string — they cannot forge tokens for other users.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import stat
import sys
from pathlib import Path
from typing import Optional

try:
    import jwt  # PyJWT
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError as exc:
    sys.stderr.write(
        f"Missing dependency: {exc.name}\n"
        "Install with: pip install 'PyJWT[crypto]>=2.8' 'cryptography>=41'\n"
        "Or install the MCP package itself: pip install qlik-sense-mcp-server\n"
    )
    raise SystemExit(2)


# ─── init-keys ──────────────────────────────────────────────────────────────


def _generate_rsa_key(bits: int) -> rsa.RSAPrivateKey:
    if bits < 2048:
        raise ValueError("RSA keys shorter than 2048 bits are not acceptable for JWT signing")
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _build_self_signed_cert(
    private_key: rsa.RSAPrivateKey,
    common_name: str,
    days_valid: int,
) -> x509.Certificate:
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "qlik-sense-mcp"),
    ])
    now = dt.datetime.now(dt.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days_valid))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key=private_key, algorithm=hashes.SHA256())
    )


def _write_secret(path: Path, data: bytes) -> None:
    if path.exists():
        raise FileExistsError(
            f"{path} already exists — delete it manually to avoid overwriting a key by accident"
        )
    path.write_bytes(data)
    try:
        # 0600 on *nix; no-op on Windows NTFS, but does not hurt.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def cmd_init_keys(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    private_path = out_dir / "jwt_private.pem"
    public_path = out_dir / "jwt_public.pem"
    cert_path = out_dir / "jwt_cert.pem"

    for p in (private_path, public_path, cert_path):
        if p.exists():
            sys.stderr.write(f"ERROR: {p} already exists. Delete old keys manually first.\n")
            return 1

    print(f"[1/3] Generating RSA-{args.bits} private key...")
    key = _generate_rsa_key(args.bits)

    print(f"[2/3] Building self-signed certificate CN={args.cn}, valid {args.cert_days} days...")
    cert = _build_self_signed_cert(key, common_name=args.cn, days_valid=args.cert_days)

    print(f"[3/3] Writing files to {out_dir}")
    _write_secret(
        private_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    public_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    cert_path.write_bytes(cert.public_bytes(encoding=serialization.Encoding.PEM))

    print()
    print("Done. Files created:")
    print(f"  PRIVATE  {private_path}   <-- secret, keep ONLY on the admin machine")
    print(f"  PUBLIC   {public_path}    (informational)")
    print(f"  CERT     {cert_path}      <-- paste into QMC JWT virtual proxy 'JWT certificate' field")
    print()
    print("Next: see docs/AUTH_JWT.md for the QMC virtual proxy setup.")
    return 0


# ─── issue-token ────────────────────────────────────────────────────────────


def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    data = path.read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(f"{path} does not contain an RSA private key")
    return key


def cmd_issue_token(args: argparse.Namespace) -> int:
    key_path = Path(args.key).resolve()
    if not key_path.is_file():
        sys.stderr.write(f"ERROR: private key not found: {key_path}\n")
        return 1

    try:
        private_key = _load_private_key(key_path)
    except Exception as exc:
        sys.stderr.write(f"ERROR: failed to load private key: {exc}\n")
        return 1

    if args.days <= 0:
        sys.stderr.write("ERROR: --days must be positive\n")
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    # Small backdate so minor clock skew between the admin machine and the
    # Qlik server does not reject a freshly-issued token.
    iat = now - dt.timedelta(seconds=30)
    exp = now + dt.timedelta(days=args.days)

    payload: dict = {
        args.user_id_claim: args.user_id,
        args.user_dir_claim: args.user_directory,
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
    }
    if args.issuer:
        payload["iss"] = args.issuer
    if args.audience:
        payload["aud"] = args.audience
    if args.jti:
        payload["jti"] = args.jti

    token = jwt.encode(payload, private_key, algorithm="RS256")

    if args.quiet:
        # Machine-readable: just the token on stdout, nothing else.
        print(token)
    else:
        print()
        print("=" * 72)
        print(f"  JWT for {args.user_directory}\\{args.user_id}")
        print(f"  Valid until: {exp.isoformat()}")
        print("=" * 72)
        print()
        print(token)
        print()
        print("Analyst mcp.json env block:")
        print()
        print('  "env": {')
        print('    "QLIK_SERVER_URL": "https://<your-qlik-host>/<prefix>",')
        print(f'    "QLIK_JWT_TOKEN": "{token}"')
        print('  }')
        print()
        print("Replace <your-qlik-host> and <prefix> with the actual Qlik")
        print("hostname and the prefix of the JWT virtual proxy you configured")
        print("in QMC (the recommended prefix is 'jwt').")
    return 0


# ─── argument parser ────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qlik_jwt_admin",
        description="Admin CLI for qlik-sense-mcp JWT authentication (keys + tokens).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init-keys
    p_init = sub.add_parser(
        "init-keys",
        help="Generate RSA keypair + self-signed certificate for the JWT virtual proxy.",
    )
    p_init.add_argument("--out", default="./jwt_keys",
                        help="Output directory (default: ./jwt_keys)")
    p_init.add_argument("--bits", type=int, default=2048,
                        help="RSA key size in bits (2048 or 4096, default 2048)")
    p_init.add_argument("--cert-days", type=int, default=3650,
                        help="Self-signed certificate validity in days (default 3650 / 10 years)")
    p_init.add_argument("--cn", default="qlik-mcp-jwt",
                        help="CommonName for the self-signed certificate (default qlik-mcp-jwt)")
    p_init.set_defaults(func=cmd_init_keys)

    # issue-token
    p_issue = sub.add_parser(
        "issue-token",
        help="Sign a JWT for a single analyst (one token = one Qlik identity).",
    )
    p_issue.add_argument("--key", required=True,
                         help="Path to jwt_private.pem produced by init-keys")
    p_issue.add_argument("--user-id", required=True,
                         help="Qlik userId (e.g. the AD sAMAccountName, 'ivanov')")
    p_issue.add_argument("--user-directory", required=True,
                         help="Qlik userDirectory (e.g. AD domain, 'COMPANY')")
    p_issue.add_argument("--days", type=int, default=365,
                         help="Token lifetime in days (default 365). Use a larger value "
                              "for long-lived service tokens, a smaller one for tighter rotation.")
    p_issue.add_argument("--user-id-claim", default="userId",
                         help="JWT claim name holding user ID — must match 'JWT attribute "
                              "for user ID' in QMC (default userId)")
    p_issue.add_argument("--user-dir-claim", default="userDirectory",
                         help="JWT claim name holding user directory — must match 'JWT "
                              "attribute for user directory' in QMC (default userDirectory)")
    p_issue.add_argument("--issuer", default=None,
                         help="Optional iss claim")
    p_issue.add_argument("--audience", default=None,
                         help="Optional aud claim")
    p_issue.add_argument("--jti", default=None,
                         help="Optional jti claim (opaque unique token id for audit trails)")
    p_issue.add_argument("--quiet", action="store_true",
                         help="Print only the token on stdout (for scripting)")
    p_issue.set_defaults(func=cmd_issue_token)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
