FROM python:3.12-alpine

RUN apk add --no-cache git

# Local test copy: install the submitter from the bundled ./submitter folder
# instead of from GitHub, so the code under test is the code in this repo.
COPY submitter /submitter
RUN python -m pip install --no-cache-dir --upgrade pip && \
	pip install --no-cache-dir /submitter

COPY --chmod=755 entrypoint.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
