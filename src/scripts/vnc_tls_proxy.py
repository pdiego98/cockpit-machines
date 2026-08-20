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

def create_ssl_context():
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    
    # Common libvirt/qemu PKI paths
    pki_paths = [
        "/etc/pki/libvirt-vnc",
        "/etc/pki/qemu"
    ]
    
    ca_loaded = False
    cert_loaded = False
    
    for path in pki_paths:
        ca_cert = os.path.join(path, "ca-cert.pem")
        client_cert = os.path.join(path, "clientcert.pem")
        client_key = os.path.join(path, "clientkey.pem")
        
        if os.path.exists(ca_cert):
            try:
                context.load_verify_locations(cafile=ca_cert)
                ca_loaded = True
            except Exception as e:
                debug(f"Failed to load CA cert {ca_cert}: {e}")
        
        if os.path.exists(client_cert) and os.path.exists(client_key):
            try:
                context.load_cert_chain(certfile=client_cert, keyfile=client_key)
                cert_loaded = True
            except Exception as e:
                debug(f"Failed to load client cert/key from {path}: {e}")
                
        if ca_loaded:
            break

    # Always disable server hostname verification because VNC targets are often IPs
    context.check_hostname = False
    
    # Optionally, we can also disable server cert verification to avoid issues with self-signed certs
    context.verify_mode = ssl.CERT_NONE

    return context

def main():
    parser = argparse.ArgumentParser(description="VNC TLS Proxy")
    parser.add_argument("host", help="Destination VNC host")
    parser.add_argument("port", type=int, help="Destination VNC port")
    args = parser.parse_args()

    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.bind(("127.0.0.1", 0))
    listen_sock.listen(1)
    
    local_port = listen_sock.getsockname()[1]
    print(local_port, flush=True)

    try:
        client_sock, _ = listen_sock.accept()
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        listen_sock.close()

    try:
        context = create_ssl_context()
        target_sock = socket.create_connection((args.host, args.port))
        
        # --- VeNCrypt Handshake Interception ---
        # 1. Server sends RFB version
        server_rfb = target_sock.recv(12)
        if not server_rfb.startswith(b"RFB "):
            raise Exception("Target is not a VNC server")
        
        # We send RFB version back to QEMU
        target_sock.sendall(server_rfb)
        
        # 2. Server sends security types
        num_sec_types = target_sock.recv(1)
        if not num_sec_types or num_sec_types == b'\x00':
            raise Exception("No security types offered or connection closed")
        num = num_sec_types[0]
        sec_types = target_sock.recv(num)
        
        VENCRYPT = 19
        if VENCRYPT not in sec_types:
            raise Exception(f"VeNCrypt (19) not supported by server. Offered: {list(sec_types)}")
            
        # 3. Select VeNCrypt
        target_sock.sendall(bytes([VENCRYPT]))
        
        # 4. VeNCrypt version exchange
        server_vencrypt_ver = target_sock.recv(4)
        target_sock.sendall(server_vencrypt_ver) # Echo back
        
        # 5. VeNCrypt status
        status = target_sock.recv(1)
        if status != b'\x00':
            raise Exception(f"VeNCrypt refused, status {status}")
            
        # 6. VeNCrypt subtypes
        num_subtypes_b = target_sock.recv(1)
        num_subtypes = num_subtypes_b[0]
        subtypes = []
        for _ in range(num_subtypes):
            subtypes.append(int.from_bytes(target_sock.recv(4), 'big'))
            
        # TLSNone (258), TLSVnc (259), TLSPlain (257)
        chosen_subtype = None
        if 258 in subtypes:
            chosen_subtype = 258
        elif 259 in subtypes:
            chosen_subtype = 259
        elif 257 in subtypes:
            chosen_subtype = 257
        else:
            raise Exception(f"No supported TLS subtype found. Offered: {subtypes}")
            
        target_sock.sendall(chosen_subtype.to_bytes(4, 'big'))
        
        # 7. Upgrade to TLS
        server_hostname = args.host
        if context.verify_mode == ssl.CERT_NONE or args.host.replace('.', '').isdigit() or ':' in args.host:
            server_hostname = None
            
        tls_sock = context.wrap_socket(target_sock, server_hostname=server_hostname)
        
        # --- Fake Handshake to noVNC ---
        # Send RFB to noVNC
        client_sock.sendall(server_rfb)
        
        # Read noVNC's RFB
        client_rfb = client_sock.recv(12)
        
        # Send fake security types to noVNC based on what we negotiated over TLS
        fake_sec_type = 1 # None
        if chosen_subtype == 259:
            fake_sec_type = 2 # VncAuth
            
        client_sock.sendall(b'\x01' + bytes([fake_sec_type]))
        
        # Read noVNC's chosen security type
        client_chosen = client_sock.recv(1)
        if client_chosen[0] != fake_sec_type:
            raise Exception(f"noVNC chose unexpected security type: {client_chosen[0]}")
            
        # The handshake is now synchronized! The TLS tunnel is established, 
        # and both QEMU and noVNC expect the post-security-negotiation phase.
        debug("Handshake synchronized successfully.")
    except Exception as e:
        debug(f"Failed to connect to VNC server {args.host}:{args.port} over TLS: {e}")
        client_sock.close()
        sys.exit(1)

    # Forward data between the sockets
    sockets = [client_sock, tls_sock]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [])
            for s in readable:
                other_s = tls_sock if s is client_sock else client_sock
                data = s.recv(4096)
                if not data:
                    return
                other_s.sendall(data)
    except Exception as e:
        debug(f"Proxy error: {e}")
    finally:
        client_sock.close()
        tls_sock.close()

if __name__ == "__main__":
    main()
