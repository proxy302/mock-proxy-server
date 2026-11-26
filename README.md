# mock-proxy-server: test your code's proxy handling without a real proxy

To test how a program copes with a proxy, run a fake one on your own machine that behaves badly on command, and point the program at it. `mock_proxy.py` is a single Python file with no dependencies. It is an HTTP proxy (CONNECT tunnels and plain `http://` forwarding) and a SOCKS5 proxy, with an optional username and password and a log line for every request. Switches make it refuse the login (407), answer 403, 502, 503 or 504, reset the connection, never answer, add latency, or close tunnels that sit idle, on every request or on a random share of them. Everything runs on localhost, so a test gives the same result on a laptop with no internet as in CI. Proxy302 maintains this tool and sells proxies. When you want to repeat a test against a real proxy, a static residential (ISP) IP costs $7.99 for 30 days, or $0.50 for one day ([prices](https://proxy302.com/pricing/)).

> Maintained by [Proxy302](https://proxy302.com). MIT licensed. Tested on macOS with Python 3.9.6, curl 8.7.1, httpx 0.28.1, aiohttp 3.13.5 and Node 24.14.

## Usage

```bash
git clone https://github.com/proxy302/mock-proxy-server
cd mock-proxy-server
python3 mock_proxy.py                                         # HTTP on 3128, SOCKS5 on 1080, no login
python3 mock_proxy.py --user alice --password s3cret          # require a login on both
python3 mock_proxy.py --user alice --password s3cret --fail 502 --fail-rate 0.3 --seed 1
python3 mock_proxy.py --latency 80 --idle-timeout 30
```

Python 3.8 or later, standard library only. It listens on 127.0.0.1 unless you pass `--bind`. It is a test tool: do not leave it open on a public address.

| Option | What it does |
|---|---|
| `--http PORT`, `--socks PORT` | Ports for the HTTP and SOCKS5 sides; `0` turns one off (defaults 3128 and 1080) |
| `--user U --password P` | Require this login: HTTP Basic (`Proxy-Authorization`) and the SOCKS5 username/password method |
| `--fail 407\|403\|502\|503\|504\|reset\|hang` | Inject that fault. On SOCKS5, 403 becomes reply 0x02, 502 becomes 0x05, 503 becomes 0x01, 504 becomes 0x06 and 407 a failed login |
| `--fail-rate R` | Inject the fault on a share of requests only (0 to 1); add `--seed N` to get the same sequence every run |
| `--latency MS` | Wait MS milliseconds before everything the proxy sends back, which behaves like a distant proxy for small exchanges (it is not a bandwidth limit) |
| `--idle-timeout S` | Close a tunnel after S seconds with no data in either direction |
| `--map NAME=IP` | Resolve NAME to IP at the proxy, so test hostnames such as `demo.test` work without touching `/etc/hosts` |
| `--echo PORT` | Also run a small HTTP server that answers with the request it received, to use as a target |
| `--connect-timeout S`, `--quiet` | Timeout for reaching the target (default 10 s); turn off the log |

When the proxy cannot reach the target for real, it answers the way proxies usually do: 502 (HTTP) or 0x04/0x05 (SOCKS5) when the name does not resolve or the port refuses, 504 or 0x06 on a timeout.

## Example: a normal session and its log

```text
$ python3 mock_proxy.py --http 26128 --socks 26180 --echo 26081 --user alice --password s3cret --map demo.test=127.0.0.1
mock-proxy listening
  HTTP proxy    http://127.0.0.1:26128
  SOCKS5 proxy  socks5h://127.0.0.1:26180
  echo target   http://127.0.0.1:26081/
  login         user 'alice', password set
  behaviour     map demo.test=127.0.0.1
```

In a second terminal (`demo.test:26443` is a local TLS test server with its own certificate, `cert.pem`):

```text
$ curl -sS -x http://alice:s3cret@127.0.0.1:26128 http://demo.test:26081/hello
echo: you are 127.0.0.1:60689
GET /hello HTTP/1.1
Host: demo.test:26081
User-Agent: curl/8.7.1
Accept: */*
Connection: close

$ curl -sS -o /dev/null -w "%{http_code}\n" -x http://alice:wrong@127.0.0.1:26128 http://demo.test:26081/
407
$ curl -sS --cacert cert.pem -x socks5h://alice:s3cret@127.0.0.1:26180 https://demo.test:26443/
hello over TLS
$ curl -sS -o /dev/null -w "%{http_code}\n" -x socks5://alice:s3cret@127.0.0.1:26180 http://127.0.0.1:26081/
200
```

What the proxy logged:

```text
10:52:51.871 http   127.0.0.1:60688 GET http://demo.test:26081/hello auth=ok -> forwarded
10:52:51.872 http   127.0.0.1:60688 closed: ended, up 0 B, down 217 B, 0.00 s; target answered: HTTP/1.1 200 OK
10:52:51.883 http   127.0.0.1:60690 GET http://demo.test:26081/ auth=wrong password -> 407
10:52:51.894 socks5 127.0.0.1:60691 CONNECT demo.test:26443 (hostname, resolved by the proxy) auth=ok -> 0x00 tunnel
10:52:51.899 socks5 127.0.0.1:60691 closed: ended, up 526 B, down 1.4 kB, 0.01 s
10:52:51.908 socks5 127.0.0.1:60693 CONNECT 127.0.0.1:26081 (IPv4, resolved by the client) auth=ok -> 0x00 tunnel
10:52:51.909 socks5 127.0.0.1:60693 closed: ended, up 78 B, down 193 B, 0.00 s
```

Two things the log shows that are hard to see from the client side. The echo server received no `Proxy-Authorization` header, so the credentials stopped at the proxy as they should. And a SOCKS5 request says whether the client sent a hostname (`socks5h://`, the proxy resolves it) or an address it had already looked up itself (`socks5://`).

## Example: what each client says for each fault

One HTTPS request per client through the HTTP side (CONNECT), plus curl through the SOCKS5 side, with the proxy started as above and `--fail` set to each value in turn. The timeout was 3 seconds. Output as printed, with trailing spaces trimmed:

```text
=== --fail none
curl http 200
curl socks 200
httpx      200
aiohttp    200
node fetch 200
=== --fail 407
curl http curl: (56) CONNECT tunnel failed, response 407 000
curl socks curl: (97) User was rejected by the SOCKS5 server (1 1).  000
httpx      ProxyError: 407 Proxy Authentication Required
aiohttp    ClientHttpProxyError: 407, message='Proxy Authentication Required', url='http://alice:s3cret@127.0.0.1:26128'
node fetch TypeError: fetch failed <- UND_ERR_ABORTED: Proxy response (407) !== 200 when HTTP Tunneling
=== --fail 403
curl http curl: (56) CONNECT tunnel failed, response 403 000
curl socks curl: (97) Can't complete SOCKS5 connection to demo.test. (2)  000
httpx      ProxyError: 403 Forbidden
aiohttp    ClientHttpProxyError: 403, message='Forbidden', url='http://alice:s3cret@127.0.0.1:26128'
node fetch TypeError: fetch failed <- UND_ERR_ABORTED: Proxy response (403) !== 200 when HTTP Tunneling
=== --fail 502
curl http curl: (56) CONNECT tunnel failed, response 502 000
curl socks curl: (97) Can't complete SOCKS5 connection to demo.test. (5)  000
httpx      ProxyError: 502 Bad Gateway
aiohttp    ClientHttpProxyError: 502, message='Bad Gateway', url='http://alice:s3cret@127.0.0.1:26128'
node fetch TypeError: fetch failed <- UND_ERR_ABORTED: Proxy response (502) !== 200 when HTTP Tunneling
=== --fail 503
curl http curl: (56) CONNECT tunnel failed, response 503 000
curl socks curl: (97) Can't complete SOCKS5 connection to demo.test. (1)  000
httpx      ProxyError: 503 Service Unavailable
aiohttp    ClientHttpProxyError: 503, message='Service Unavailable', url='http://alice:s3cret@127.0.0.1:26128'
node fetch TypeError: fetch failed <- UND_ERR_ABORTED: Proxy response (503) !== 200 when HTTP Tunneling
=== --fail 504
curl http curl: (56) CONNECT tunnel failed, response 504 000
curl socks curl: (97) Can't complete SOCKS5 connection to demo.test. (6)  000
httpx      ProxyError: 504 Gateway Timeout
aiohttp    ClientHttpProxyError: 504, message='Gateway Timeout', url='http://alice:s3cret@127.0.0.1:26128'
node fetch TypeError: fetch failed <- UND_ERR_ABORTED: Proxy response (504) !== 200 when HTTP Tunneling
=== --fail reset
curl http curl: (56) Recv failure: Connection reset by peer 000
curl socks curl: (97) Recv failure: Connection reset by peer  000
httpx      ReadError: [Errno 54] Connection reset by peer
aiohttp    ClientOSError: [Errno 54] Connection reset by peer
node fetch TypeError: fetch failed <- ECONNRESET: read ECONNRESET
=== --fail hang
curl http curl: (28) Connection timed out after 3006 milliseconds 000
curl socks curl: (28) Connection timed out after 3008 milliseconds  000
httpx      ReadTimeout: timed out
aiohttp    TimeoutError:
node fetch TimeoutError: The operation was aborted due to timeout
```

The clients were curl with `-x`, `httpx.get(url, proxy=...)`, `aiohttp` with `proxy=...` and Node's built-in `fetch` with `NODE_USE_ENV_PROXY=1` and `HTTPS_PROXY` set; the Node line prints the error and, after `<-`, its innermost `cause`. What we noticed:

- **The status code is there, but you have to dig for it.** httpx and aiohttp put it in the exception; curl puts it in the message text; Node's `fetch` only says `fetch failed`, and the proxy's status sits two `cause` levels down.
- **aiohttp's error message contains the proxy URL with the password.** Mask it before you log exceptions.
- **SOCKS5 has no 502 or 407.** curl reports the SOCKS reply code in brackets: 2 means the proxy's rules refused, 5 that the target refused, 6 a timeout, 1 a general failure.
- **A hang and a reset look different everywhere,** which is why a retry policy can treat them differently: a timeout may mean the proxy is overloaded or unreachable, while a reset came back immediately.

## Example: a flaky proxy and a retry loop

Start the proxy so that half of the CONNECT requests fail with 502, with a fixed seed so the run is repeatable:

```bash
python3 mock_proxy.py --http 26128 --socks 0 --user alice --password s3cret \
  --map demo.test=127.0.0.1 --fail 502 --fail-rate 0.5 --seed 7
```

```python
import ssl, time, httpx

PROXY = "http://alice:s3cret@127.0.0.1:26128"
RETRY_ON = ("502", "503", "504")
tls = ssl.create_default_context(cafile="cert.pem")   # the test site's self-signed certificate

def get(url, attempts=4):
    for n in range(1, attempts + 1):
        try:
            r = httpx.get(url, proxy=PROXY, verify=tls, timeout=5)
            print(f"  attempt {n}: site answered {r.status_code}")
            return r
        except httpx.ProxyError as e:          # the proxy refused the CONNECT
            print(f"  attempt {n}: proxy answered {e}")
            if not str(e).startswith(RETRY_ON):
                raise                           # 407 or 403: retrying will not help
        time.sleep(0.2 * 2 ** (n - 1))
    raise RuntimeError("gave up")

for i in range(1, 4):
    print(f"request {i}")
    get("https://demo.test:26443/")
```

```text
request 1
  attempt 1: proxy answered 502 Bad Gateway
  attempt 2: proxy answered 502 Bad Gateway
  attempt 3: site answered 200
request 2
  attempt 1: proxy answered 502 Bad Gateway
  attempt 2: site answered 200
request 3
  attempt 1: proxy answered 502 Bad Gateway
  attempt 2: proxy answered 502 Bad Gateway
  attempt 3: site answered 200
```

The proxy's log shows the back-off between attempts (0.2 s, then 0.4 s):

```text
10:49:10.901 http   127.0.0.1:59453 CONNECT demo.test:26443 auth=ok -> 502 (injected)
10:49:11.111 http   127.0.0.1:59455 CONNECT demo.test:26443 auth=ok -> 502 (injected)
10:49:11.516 http   127.0.0.1:59457 CONNECT demo.test:26443 auth=ok -> 200 tunnel
```

For a plain `http://` URL there is no CONNECT, so the proxy's 502 reaches httpx as an ordinary response with status 502, not as a `ProxyError`. The only sign that it came from the proxy is in the headers (here `Server: mock-proxy`). Test both kinds of URL if your code handles both.

## Example: an idle limit

```bash
python3 mock_proxy.py --http 26128 --socks 0 --echo 26081 --idle-timeout 5
```

```python
import socket, time
s = socket.create_connection(("127.0.0.1", 26128))
s.sendall(b"CONNECT 127.0.0.1:26081 HTTP/1.1\r\nHost: 127.0.0.1:26081\r\n\r\n")
print(s.recv(200).decode().strip())
t = time.time()
s.settimeout(60)
data = s.recv(100)              # send nothing and wait
print("tunnel closed after %.1f s of silence" % (time.time() - t) if not data else data)
```

```text
HTTP/1.1 200 Connection established
tunnel closed after 5.1 s of silence
```

```text
10:52:58.715 http   127.0.0.1:60701 CONNECT 127.0.0.1:26081 auth=not required -> 200 tunnel
10:53:03.810 http   127.0.0.1:60701 closed: idle limit (5 s without data), closed by proxy, up 0 B, down 0 B, 5.10 s
```

Use this to check that your WebSocket or database client sends keep-alive traffic often enough, and reconnects when a connection is cut anyway.

## Limits

- SOCKS5 supports CONNECT only. UDP ASSOCIATE and BIND get reply 0x07 (command not supported).
- Plain `http://` forwarding uses one connection to the target per request (`Connection: close`). Tunnels keep whatever the client and site agree.
- Only Basic authentication on the HTTP side, and no TLS between client and proxy (`https://` proxy URLs are not supported).
- `--latency` delays messages; it does not limit bandwidth. TCP connects to the proxy itself are not delayed.

## Related

- [proxy-error-diagnose](https://github.com/proxy302/proxy-error-diagnose): find the step where a real proxy fails
- [How do I use a proxy with httpx and aiohttp?](https://github.com/proxy302/proxy-guides/blob/main/httpx-aiohttp-proxy/README.md)
- [How do I use a proxy in Node.js?](https://github.com/proxy302/proxy-guides/blob/main/nodejs-proxy/README.md)
- [What does ERR_TUNNEL_CONNECTION_FAILED mean?](https://github.com/proxy302/proxy-guides/blob/main/err-tunnel-connection-failed/README.md)

*Checked 26 November 2026.*
