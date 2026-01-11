import argparse
import os
from app import create_app, socketio

def main():
    parser = argparse.ArgumentParser(description='RunPod2Drive - Upload files to Google Drive')
    parser.add_argument('-p', '--port', type=int, default=7860,
                        help='Port to run the web UI (default: 7860)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('--debug', action='store_true',
                        help='Run in debug mode')

    args = parser.parse_args()

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
