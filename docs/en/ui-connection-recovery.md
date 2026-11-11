# UI connection recovery

A server restart or a broken connection can leave a browser request waiting without a response. This must not freeze progress at an old value or leave job activity connecting indefinitely.

JSON reads (GET and HEAD) have a 25-second deadline, including response-body decoding. Caller cancellation still works. When a read times out, the status poll and job activity feed release their pending request and their next scheduled read can recover without reloading the page. The last visible progress is retained with an update error; a connection failure does not mean zero progress or job failure.

Status errors are separate from errors raised by collection actions. A successful status update clears its own error and preserves unrelated action errors. If the reverse proxy returns a plain-text 404 without an application response, the UI explains that the service is unavailable. After an uncertain write, inspect Processing before repeating the action.

The client does not automatically replay writes because of a timeout, proxy failure or lost connection. The existing one-time renewal of an explicitly rejected open-mode CSRF token is unchanged. Authentication, tracing, job admission and collection acceptance rules are unchanged.

Regression checks cover stalled headers, stalled JSON bodies with caller cancellation, cancellation cleanup, a following successful read, activity recovery without reloading, and no write replay on a proxy 404. A server restart's underlying cause requires separate runtime evidence; recovery tests do not establish an OOM diagnosis.
