"""WSL-only launcher using the existing projected mTLS server conventions."""
import argparse
import ssl
import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Unprivileged loopback port required')
    from .runtime import app
    from .security import ProjectedReceiver
    receiver = app.state.managed_receiver
    if type(receiver) is not ProjectedReceiver:
        raise SystemExit('Explicit projected WSL OFF receiver required')
    config = uvicorn.Config(app, host='127.0.0.1', port=args.port, workers=1,
                            proxy_headers=False, access_log=False,
                            **receiver.transport.server_options())
    config.load()
    config.ssl.minimum_version = ssl.TLSVersion.TLSv1_3
    config.ssl.set_alpn_protocols(['http/1.1'])
    uvicorn.Server(config).run()


if __name__ == '__main__':
    main()
