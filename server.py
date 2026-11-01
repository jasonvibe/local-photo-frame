"""Run the photo frame with a small, cross-platform WSGI server."""
import argparse
from waitress import serve
from app import app


def main():
    parser = argparse.ArgumentParser(description='Local Photo Frame')
    parser.add_argument('--host', default='127.0.0.1', help='Use 0.0.0.0 for trusted LAN access')
    parser.add_argument('--port', type=int, default=4444)
    args = parser.parse_args()
    print(f'Photo frame: http://localhost:{args.port}/', flush=True)
    print(f'Management: http://localhost:{args.port}/upload', flush=True)
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == '__main__':
    main()
