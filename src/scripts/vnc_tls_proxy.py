#!/usr/bin/env python3
# Proxy for VNC over TLS, used by Cockpit-Machines
# Listens on a local loopback port and forwards traffic via TLS to the VNC port.
import argparse
import socket
import ssl
import sys
import select
import os

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
                print(f"Failed to load CA cert {ca_cert}: {e}", file=sys.stderr)
        
        if os.path.exists(client_cert) and os.path.exists(client_key):
            try:
                context.load_cert_chain(certfile=client_cert, keyfile=client_key)
                cert_loaded = True
            except Exception as e:
                print(f"Failed to load client cert/key from {path}: {e}", file=sys.stderr)
                
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

    # Listen on localhost, random port
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.bind(("127.0.0.1", 0))
    listen_sock.listen(1)
    
    local_port = listen_sock.getsockname()[1]
    # Print the local port to stdout so the parent process knows where to connect
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
        
        server_hostname = args.host
        # If verify_mode is CERT_NONE or host is an IP address, don't pass server_hostname
        # to avoid ValueError: server_hostname cannot be an IP address
        if context.verify_mode == ssl.CERT_NONE or args.host.replace('.', '').isdigit() or ':' in args.host:
            server_hostname = None
            
        tls_sock = context.wrap_socket(target_sock, server_hostname=server_hostname)
    except Exception as e:
        print(f"Failed to connect to VNC server {args.host}:{args.port} over TLS: {e}", file=sys.stderr)
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
                    # Connection closed
                    return
                other_s.sendall(data)
    except Exception as e:
        print(f"Proxy error: {e}", file=sys.stderr)
    finally:
        client_sock.close()
        tls_sock.close()

if __name__ == "__main__":
    main()
