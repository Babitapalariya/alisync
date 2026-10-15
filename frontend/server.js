const http = require("http");
const fs = require("fs");
const path = require("path");

const port = 3002;
http.createServer((req, res) => {
  const pathname = new URL(req.url, "http://localhost").pathname;
  const policyPaths = ["/privacy-policy", "/privacy-policy/", "/privacy-policy.html"];
  const file = policyPaths.includes(pathname) ? "privacy-policy.html" : "index.html";
  fs.readFile(path.join(__dirname, file), (error, data) => {
    if (error) { res.writeHead(500); res.end("AliSync frontend not found"); return; }
    res.writeHead(200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"});
    res.end(data);
  });
}).listen(port, () => console.log(`AliSync frontend: http://localhost:${port}`));
