"""Publisher-side license key generator. Dev tool only — the installed app never signs.

Usage:
  python scripts/make_license.py --generate-keypair --private-key-file ~/.keys/finv-license.pem
  python scripts/make_license.py --private-key-file ~/.keys/finv-license.pem --tier pro --customer "Acme" --expires 2027-09-16
  python scripts/make_license.py ... --max-asset-classes 2 --max-value 500000 --value-currency USD

Tiers are tracking limits: ``standard`` covers 1 asset class, ``pro`` 5.
The --max-* flags override the tier defaults in the signed payload, so custom
deals (e.g. 3 classes, capped at $250k tracked value) need no code change.
The private key is written to a 0600 file and is never printed.
Replace licensing.PUBLIC_KEY with the printed public key before release.
"""

import argparse
import base64
import os
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

import frappe_investing.licensing as licensing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate-keypair", action="store_true")
    parser.add_argument("--private-key-file", required=True)
    parser.add_argument("--tier", default="pro", choices=list(licensing._TIER_LIMITS))
    parser.add_argument("--customer")
    parser.add_argument("--expires")
    parser.add_argument(
        "--max-asset-classes",
        type=int,
        default=None,
        help="Override the tier's class limit. Omit to use the tier default.",
    )
    parser.add_argument("--max-value", default=None, help="Portfolio value cap, decimal string.")
    parser.add_argument("--value-currency", default=None, help="ISO currency for --max-value.")
    args = parser.parse_args()
    key_path = Path(args.private_key_file).expanduser()
    if args.generate_keypair:
        if key_path.exists():
            parser.error(f"Refusing to overwrite existing key: {key_path}")
        private = Ed25519PrivateKey.generate()
        pem = private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        key_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        print(f"Private key written to {key_path} (0600). Never commit it.")
        print("PUBLIC_KEY for licensing.py:", base64.urlsafe_b64encode(public).decode())
        return
    if not args.customer or not args.expires:
        parser.error("signing needs --customer and --expires")
    if args.max_value and not args.value_currency:
        parser.error("--max-value needs --value-currency")
    if args.max_asset_classes is not None and args.max_asset_classes < 1:
        parser.error("--max-asset-classes must be at least 1")
    private = load_pem_private_key(key_path.read_bytes(), password=None)
    payload = {
        "product": licensing.PRODUCT,
        "tier": args.tier,
        "customer": args.customer,
        "issued": date.today().isoformat(),
        "expires": args.expires,
    }
    if args.max_asset_classes is not None:
        payload["max_asset_classes"] = args.max_asset_classes
    if args.max_value is not None:
        payload["max_value"] = str(args.max_value)
        payload["value_currency"] = args.value_currency.upper()
    print(licensing.sign_license(payload, private))


if __name__ == "__main__":
    main()
