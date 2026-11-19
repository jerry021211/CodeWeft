"""Create a LAN-specific TLS identity without retaining a CA signing key."""
import ipaddress
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def lan_addresses():
    networks = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    addresses = {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    return sorted(addr for addr in addresses if any(ipaddress.ip_address(addr) in net for net in networks))


def identity(root: Path, address: str):
    folder = root / "mobile-tls" / str(ipaddress.IPv4Address(address))
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    ca, leaf, key = (folder / name for name in ("CodeWeft.cer", "server.pem", "server.key"))
    now = datetime.now(timezone.utc)
    if ca.exists() and leaf.exists() and key.exists():
        cert = x509.load_pem_x509_certificate(leaf.read_bytes())
        if cert.not_valid_after_utc > now + timedelta(days=1):
            return ca, leaf, key

    signer = ec.generate_private_key(ec.SECP256R1())
    server = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"CodeWeft {address} local CA")])
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)])
    def certificate(name, public_key):
        return (x509.CertificateBuilder().subject_name(name).issuer_name(issuer)
                .public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365)))
    authority = (certificate(issuer, signer.public_key())
                 .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                 .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
                 .sign(signer, hashes.SHA256()))
    endpoint = (certificate(subject, server.public_key())
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(signer, hashes.SHA256()))
    # Only the endpoint private key is persisted. Trusting the public CA cannot
    # enable this workbench to issue certificates for any other service later.
    with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
        out.write(server.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    leaf.write_bytes(endpoint.public_bytes(serialization.Encoding.PEM))
    ca.write_bytes(authority.public_bytes(serialization.Encoding.DER))
    return ca, leaf, key
