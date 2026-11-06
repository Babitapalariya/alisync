const http = require("http");
const fs = require("fs");
const path = require("path");

const port = 3002;
http.createServer((req, res) => {
  const pathname = new URL(req.url, "http://localhost").pathname;
  const policyPaths = ["/privacy-policy", "/privacy-policy/", "/privacy-policy.html"];
  const assets = {
    "/orders.css": ["orders.css", "text/css; charset=utf-8"],
    "/orders.js": ["orders.js", "application/javascript; charset=utf-8"],
  };
  const asset = Object.prototype.hasOwnProperty.call(assets, pathname) ? assets[pathname] : null;
  const file = asset ? asset[0] : policyPaths.includes(pathname) ? "privacy-policy.html" : "index.html";
  const contentType = asset ? asset[1] : "text/html; charset=utf-8";
  fs.readFile(path.join(__dirname, file), (error, data) => {
    if (error) { res.writeHead(500); res.end("AliSync frontend not found"); return; }
    res.writeHead(200, {"Content-Type": contentType, "Cache-Control": "no-store"});
    res.end(data);
  });
}).listen(port, () => console.log(`AliSync frontend: http://localhost:${port}`));
