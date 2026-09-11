# Security

Do not expose the runner API anonymously. Set `QA_API_TOKEN`, protect the Worker with Cloudflare Access, apply rate limits, and run the browser container without access to private networks or cloud metadata endpoints.

The API rejects non-public resolved IP addresses, embedded credentials, unsupported schemes, Markdown-formatted URLs, and uploads above the configured limit. DNS rebinding and application-level abuse still require infrastructure egress controls.

Only test applications you own or are explicitly authorized to assess. Form submissions, API writes, mutation testing, security testing, and load testing require explicit scope and authority.
