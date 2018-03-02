#!/usr/bin/env python3
import sys
import select
import socket

IAC = bytes([255])  # "Interpret As Command"
SB = bytes([250])  # Subnegotiation Begin
VMWARE_EXT = bytes([232])
VM_VC_UUID = bytes([80])
SE = bytes([240])  # Subnegotiation End


def send_vm_uuid(sock):
    sock.sendall(IAC + SB + VMWARE_EXT + VM_VC_UUID + "11-22".encode('ascii') + IAC + SE)


def main(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    send_vm_uuid(sock)

    while True:
        ready = select.select([sys.stdin, sock], [], [], 0.1)[0]
        if sys.stdin in ready:
            line = sys.stdin.readline()
            if line:
                sock.sendall(line.encode('ascii'))
            else:
                sys.exit(0)
        elif sock in ready:
            data = sock.recv(1024)
            if not data:
                sys.exit(0)
            print(data.decode('ascii'), end='')

if __name__ == '__main__':
    try:
        main('localhost', 13370)
    except KeyboardInterrupt:
        pass