const http = require("http");
const fs = require("fs");
const path = require("path");

const port = 3002;
http.createServer((req, res) => {
  fs.readFile(path.join(__dirname, "index.html"), (error, data) => {
    if (error) { res.writeHead(500); res.end("AliSync frontend not found"); return; }
    res.writeHead(200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"});
    res.end(data);
  });
}).listen(port, () => console.log(`AliSync frontend: http://localhost:${port}`));
