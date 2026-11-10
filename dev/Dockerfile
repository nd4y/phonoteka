# Appended to the root Dockerfile by build01_host.py to reuse its runtime stage.
FROM runtime AS demo
USER 0:0
RUN pip install -c /app/requirements.lock pytest==9.0.3
COPY tests/ /app/tests/
COPY dev/demo.py dev/install-ci-codecs.sh dev/ubuntu-ci.sources /app/dev/
USER 10001:10001
CMD ["python", "/app/dev/demo.py"]
