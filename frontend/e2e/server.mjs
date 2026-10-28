import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve("dist");
const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml" };

export function startStaticServer(port) {
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, `http://${request.headers.host}`);
    if (url.pathname.startsWith("/api/v1/")) {
      const proxy = http.request({
        hostname: "127.0.0.1",
        port: 8001,
        path: url.pathname + url.search,
        method: request.method,
        headers: request.headers,
      }, (upstream) => {
        response.writeHead(upstream.statusCode, upstream.headers);
        upstream.pipe(response);
      });
      proxy.on("error", () => {
        response.writeHead(502, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ detail: "本机后端不可用" }));
      });
      request.pipe(proxy);
      return;
    }
    const requested = path.resolve(root, `.${decodeURIComponent(url.pathname)}`);
    const file = requested.startsWith(root) && fs.existsSync(requested) && fs.statSync(requested).isFile() ? requested : path.join(root, "index.html");
    response.writeHead(200, { "Content-Type": types[path.extname(file)] || "application/octet-stream" });
    fs.createReadStream(file).pipe(response);
  });
  return new Promise((resolve) => server.listen(port, "127.0.0.1", () => resolve(server)));
}
