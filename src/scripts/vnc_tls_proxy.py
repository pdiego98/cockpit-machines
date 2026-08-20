#!/usr/bin/env python3
# Proxy for VNC over TLS, used by Cockpit-Machines
# Listens on a local loopback port and forwards traffic via TLS to the VNC port.
import argparse
import socket
import ssl
import sys
import select
import os

def debug(msg):
    with open("/tmp/cockpit_vnc_tls_proxy.log", "a") as f:
        print(msg, file=f)

def create_ssl_context(args):
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    
    # PKI paths used by libvirt/QEMU for VNC TLS
    # Filenames follow libvirt convention: client-cert.pem / client-key.pem
    pki_paths = [
        args.cert_dir,
        "/etc/pki/libvirt-vnc",
        "/etc/pki/qemu",
    ]
    
    # Try both hyphenated (libvirt style) and non-hyphenated (older style) names
    cert_name_pairs = [
        ("client-cert.pem", "client-key.pem"),
        ("clientcert.pem",  "clientkey.pem"),
    ]
    ca_names = ["ca-cert.pem", "ca-cert.pem"]  # same in both styles

    ca_loaded = False
    cert_loaded = False

    for pki_path in pki_paths:
        debug(f"Checking PKI path: {pki_path}")

        # Try to load CA cert
        ca_cert = os.path.join(pki_path, "ca-cert.pem")
        if os.path.exists(ca_cert):
            debug(f"  Found CA cert: {ca_cert}")
            try:
                context.load_verify_locations(cafile=ca_cert)
                ca_loaded = True
                debug(f"  Loaded CA cert OK")
            except Exception as e:
                debug(f"  Failed to load CA cert: {e}")
        else:
            debug(f"  CA cert not found: {ca_cert}")

        # Try to load client cert + key with all name variants
        for cert_name, key_name in cert_name_pairs:
            client_cert = os.path.join(pki_path, cert_name)
            client_key  = os.path.join(pki_path, key_name)
            debug(f"  Checking client cert: {client_cert} (exists={os.path.exists(client_cert)})")
            debug(f"  Checking client key:  {client_key}  (exists={os.path.exists(client_key)})")
            if os.path.exists(client_cert) and os.path.exists(client_key):
                try:
                    context.load_cert_chain(certfile=client_cert, keyfile=client_key)
                    cert_loaded = True
                    debug(f"  Loaded client cert+key OK: {cert_name} / {key_name}")
                    break
                except Exception as e:
                    debug(f"  Failed to load client cert+key ({cert_name}): {e}")

        if ca_loaded and cert_loaded:
            break

    if not cert_loaded:
        debug("WARNING: No client certificate found. QEMU may reject the connection if vnc_tls_x509_verify=1.")
        debug("         Place client-cert.pem and client-key.pem in /etc/pki/libvirt-vnc/ or /etc/pki/qemu/")

    # Disable server hostname / cert verification (targets are IPs, may be self-signed)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    return context


def recv_exact(sock, n, label):
    """Receive exactly n bytes, logging each chunk."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"Connection closed while reading {label} (got {len(buf)}/{n} bytes)")
        buf += chunk
    debug(f"  recv [{label}]: {buf.hex()}")
    return buf

def main():
    parser = argparse.ArgumentParser(description="VNC TLS Proxy")
    parser.add_argument("--cert-dir", default="/etc/pki/libvirt-vnc", help="VNC TLS certificate directory")
    parser.add_argument("host", help="Destination VNC host")
    parser.add_argument("port", type=int, help="Destination VNC port")
    args = parser.parse_args()

    debug(f"=== VNC TLS Proxy starting: {args.host}:{args.port} ===")

    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.bind(("127.0.0.1", 0))
    listen_sock.listen(1)
    
    local_port = listen_sock.getsockname()[1]
    print(local_port, flush=True)
    debug(f"Listening on 127.0.0.1:{local_port}")

    try:
        client_sock, client_addr = listen_sock.accept()
        debug(f"noVNC client connected from {client_addr}")
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        listen_sock.close()

    try:
        context = create_ssl_context(args)
        target_sock = socket.create_connection((args.host, args.port))
        debug(f"Connected to QEMU {args.host}:{args.port}")
        
        # --- VeNCrypt Handshake with QEMU ---

        # 1. Server sends RFB version (12 bytes: "RFB 003.008\n")
        server_rfb = recv_exact(target_sock, 12, "QEMU RFB version")
        if not server_rfb.startswith(b"RFB "):
            raise Exception(f"Target is not a VNC server: {server_rfb!r}")
        debug(f"QEMU RFB version: {server_rfb!r}")
        # Echo back the same version string
        target_sock.sendall(server_rfb)
        debug(f"Sent RFB version to QEMU: {server_rfb!r}")
        
        # 2. Server sends security types (1-byte count + count bytes)
        num_sec_types_b = recv_exact(target_sock, 1, "QEMU num_sec_types")
        if num_sec_types_b == b'\x00':
            # RFB error: server sends 0 followed by error string
            reason_len = int.from_bytes(recv_exact(target_sock, 4, "error len"), 'big')
            reason = target_sock.recv(reason_len)
            raise Exception(f"QEMU reported error: {reason!r}")
        num = num_sec_types_b[0]
        sec_types = recv_exact(target_sock, num, "QEMU sec_types")
        debug(f"QEMU security types: {list(sec_types)}")
        
        VENCRYPT = 19
        if VENCRYPT not in sec_types:
            raise Exception(f"VeNCrypt (19) not offered by QEMU. Offered: {list(sec_types)}")
            
        # 3. Select VeNCrypt
        target_sock.sendall(bytes([VENCRYPT]))
        debug("Selected VeNCrypt (19)")
        
        # 4. VeNCrypt version exchange (2 bytes: major, minor)
        server_vencrypt_ver = recv_exact(target_sock, 2, "QEMU VeNCrypt version")
        debug(f"QEMU VeNCrypt version: {server_vencrypt_ver[0]}.{server_vencrypt_ver[1]}")
        # Send same version back (we support up to what server offers)
        target_sock.sendall(server_vencrypt_ver)
        debug(f"Sent VeNCrypt version: {server_vencrypt_ver.hex()}")
        
        # 5. VeNCrypt status (1 byte: 0=OK)
        status = recv_exact(target_sock, 1, "QEMU VeNCrypt status")
        if status != b'\x00':
            raise Exception(f"QEMU rejected VeNCrypt version, status={status.hex()}")
        debug("QEMU accepted VeNCrypt version")
            
        # 6. VeNCrypt subtypes (1-byte count + count*4-byte ints)
        num_subtypes_b = recv_exact(target_sock, 1, "QEMU num_subtypes")
        num_subtypes = num_subtypes_b[0]
        subtypes = []
        for i in range(num_subtypes):
            st_bytes = recv_exact(target_sock, 4, f"QEMU subtype[{i}]")
            subtypes.append(int.from_bytes(st_bytes, 'big'))
        debug(f"QEMU VeNCrypt subtypes: {subtypes}")
            
        # VeNCrypt subtypes:
        #   TLS (anonymous DH): TLSNone=257, TLSVnc=258, TLSPlain=259
        #   X.509:              X509None=260, X509Vnc=261, X509Plain=262
        # Priority: prefer *None (no extra auth), then *Vnc, then *Plain
        SUBTYPE_PRIORITY = [260, 257, 261, 258, 262, 259]
        chosen_subtype = None
        for pref in SUBTYPE_PRIORITY:
            if pref in subtypes:
                chosen_subtype = pref
                break
        if chosen_subtype is None:
            raise Exception(f"No supported VeNCrypt subtype. Offered: {subtypes}")

        debug(f"Choosing subtype {chosen_subtype}")
        target_sock.sendall(chosen_subtype.to_bytes(4, 'big'))

        # 7. Server ACKs subtype (1 byte: 1=OK, 0=rejected)
        ack = recv_exact(target_sock, 1, "QEMU subtype ACK")
        if ack != b'\x01':
            raise Exception(f"QEMU rejected subtype {chosen_subtype}, ack={ack.hex()}")
        debug(f"QEMU ACKed subtype {chosen_subtype}")

        # 8. Upgrade TCP connection to TLS
        server_hostname = args.host
        if context.verify_mode == ssl.CERT_NONE or args.host.replace('.', '').isdigit() or ':' in args.host:
            server_hostname = None

        tls_sock = context.wrap_socket(target_sock, server_hostname=server_hostname)
        debug(f"TLS handshake complete with {args.host}:{args.port}")

        # --- Fake VNC Handshake to noVNC ---
        # noVNC does not speak VeNCrypt, so we present a plain VNC handshake
        # and relay the already-established TLS connection underneath.

        # Step A: Send QEMU's RFB version to noVNC
        client_sock.sendall(server_rfb)
        debug(f"Sent RFB version to noVNC: {server_rfb!r}")

        # Step B: Read noVNC's RFB version response
        client_rfb = recv_exact(client_sock, 12, "noVNC RFB version")
        debug(f"noVNC RFB version: {client_rfb!r}")

        # Step C: Send security type list to noVNC
        # For *None subtypes: no password needed → offer security type 1 (None)
        # For *Vnc subtypes:  password needed    → offer security type 2 (VncAuth)
        if chosen_subtype in (258, 261):  # TLSVnc, X509Vnc
            fake_sec_type = 2  # VncAuth - noVNC will ask for password
        else:
            fake_sec_type = 1  # None
        
        sec_list = b'\x01' + bytes([fake_sec_type])
        client_sock.sendall(sec_list)
        debug(f"Sent security type list to noVNC: {sec_list.hex()} (type={fake_sec_type})")
        
        # Step D: Read noVNC's security type choice
        client_chosen = client_sock.recv(1)
        if not client_chosen:
            raise Exception("noVNC closed connection before choosing security type")
        debug(f"noVNC chose security type: {client_chosen.hex()}")
        if client_chosen[0] != fake_sec_type:
            raise Exception(f"noVNC chose unexpected security type {client_chosen[0]}, expected {fake_sec_type}")
            
        debug("Fake handshake with noVNC complete. Entering passthrough mode.")

    except Exception as e:
        debug(f"SETUP ERROR: {e}")
        client_sock.close()
        sys.exit(1)

    # Passthrough: forward all raw bytes between noVNC and QEMU
    sockets = [client_sock, tls_sock]
    bytes_fwd = {id(client_sock): 0, id(tls_sock): 0}
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [])
            for s in readable:
                other_s = tls_sock if s is client_sock else client_sock
                data = s.recv(4096)
                if not data:
                    direction = "noVNC→QEMU" if s is client_sock else "QEMU→noVNC"
                    debug(f"Connection closed ({direction}). Bytes fwd noVNC→QEMU={bytes_fwd[id(client_sock)]}, QEMU→noVNC={bytes_fwd[id(tls_sock)]}")
                    return
                direction = "noVNC→QEMU" if s is client_sock else "QEMU→noVNC"
                bytes_fwd[id(s)] += len(data)
                total = bytes_fwd[id(s)]
                # Log first few transfers with hex dumps to diagnose handshake
                if total <= 256:
                    debug(f"  fwd {direction} ({len(data)} bytes, total={total}): {data[:32].hex()}")
                other_s.sendall(data)
    except Exception as e:
        debug(f"Proxy forward error: {e}")
    finally:
        debug(f"Proxy exiting. Total fwd: noVNC→QEMU={bytes_fwd[id(client_sock)]}, QEMU→noVNC={bytes_fwd[id(tls_sock)]}")
        client_sock.close()
        tls_sock.close()

if __name__ == "__main__":
    main()
