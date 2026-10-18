"""Container entrypoint: one process, JSON stdout, no unsafe access log."""
import sys
from .telemetry import configure_logging, log


def main():
    service = sys.argv[1] if len(sys.argv)>1 else 'app'
    if service not in {'app','inference'}: raise SystemExit('Expected app or inference')
    configure_logging(service)
    log('service_starting')
    import uvicorn
    uvicorn.run('phonoteka.'+service+':app',host='0.0.0.0',port=8000 if service=='app' else 8001,
                proxy_headers=False,access_log=False,log_config=None)


if __name__ == '__main__': main()
