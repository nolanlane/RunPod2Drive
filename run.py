import eventlet
eventlet.monkey_patch()

import argparse
import os
from app import create_app, socketio

def main():
    parser = argparse.ArgumentParser(description='RunPod2Drive - Upload files to Google Drive')
    # Default port changed to 8080 as requested
    parser.add_argument('-p', '--port', type=int, default=8080,
                        help='Port to run the web UI (default: 8080)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('--debug', action='store_true',
                        help='Run in debug mode')

    args = parser.parse_args()

    # Check env var if port not explicitly set via CLI (or rather, allow env var to be default if not passed)
    # Since argparse handles defaults, we need to check if the user *provided* the argument.
    # However, standard practice: Env Var < CLI.
    # But argparse default overrides env var if we just do args.port.
    # So we check if RUNPOD2DRIVE_PORT is set and use it if it is, UNLESS CLI arg is passed.
    # But checking if CLI arg is passed is tricky.
    # Simpler: use env var as default in add_argument if available.

    env_port = os.environ.get('RUNPOD2DRIVE_PORT')
    if env_port:
        # If env var exists, it becomes the "default" if user doesn't specify -p
        # But we already parsed args. Let's re-parse or just override if we determine we should.
        # Actually, if we use default=8080 in argparse, we can't easily distinguish.
        # Let's check sys.argv for -p/--port. If not present, use env var.
        import sys
        if not any(x in sys.argv for x in ['-p', '--port']) and env_port.isdigit():
            args.port = int(env_port)

    app = create_app()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                      RunPod2Drive                            ║
║          Upload your RunPod files to Google Drive            ║
╠══════════════════════════════════════════════════════════════╣
║  Starting server on http://{args.host}:{args.port}                    ║
║                                                              ║
║  Make sure credentials.json is in the same directory.       ║
╚══════════════════════════════════════════════════════════════╝
""")

    # Check for credentials
    if not os.path.exists('credentials.json') and not os.path.exists('/workspace/credentials.json'):
        print(f"⚠️  Warning: credentials.json not found!")
        print("   Please add your Google OAuth credentials file.")

    socketio.run(app, host=args.host, port=args.port, debug=args.debug, allow_unsafe_werkzeug=True)

if __name__ == '__main__':
    main()
