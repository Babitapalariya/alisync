const fs = require('node:fs');
const path = require('node:path');

// Keep the deployed single-file frontend independent of static-asset routing.
const root = __dirname;
const indexPath = path.join(root, 'index.html');
let html = fs.readFileSync(indexPath, 'utf8');
for (const [name, tag, original] of [
  ['css', 'style', /<link rel="stylesheet" href="\/?orders\.css">/],
  ['js', 'script', /<script src="\/?orders\.js"><\/script>/],
]) {
  const source = fs.readFileSync(path.join(root, `orders.${name}`), 'utf8').trim();
  if (new RegExp(`</${tag}`, 'i').test(source)) throw new Error(`Unsafe closing ${tag} tag in orders.${name}`);
  const start = `<!-- BEGIN GENERATED ORDERS ${name.toUpperCase()} -->`;
  const end = `<!-- END GENERATED ORDERS ${name.toUpperCase()} -->`;
  const existing = new RegExp(`${start}[\\s\\S]*?${end}`);
  const target = existing.test(html) ? existing : original;
  if (!target.test(html)) throw new Error(`Orders ${name} insertion point missing`);
  html = html.replace(target, () => `${start}\n<${tag} id="orders-${name}-bundle">\n${source}\n</${tag}>\n${end}`);
}
fs.writeFileSync(indexPath, html);
console.log('Bundled Orders CSS and JavaScript into frontend/index.html');
