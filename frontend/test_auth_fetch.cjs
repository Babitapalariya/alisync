const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/index.html', 'utf8');
const wrapper = source.slice(source.indexOf('  window.fetch=async'), source.indexOf('  async function tenantJson'));
async function check(detail, expectedLogout) {
  let logout = false;
  let sent;
  const context = {Headers, window:{}, API:'https://app.test/app', tenantToken:'account-token',
    activeShopId:'7', shopifyEmbedded:true, embeddedToken:async()=> 'shopify-token',
    nativeFetch:async(input, options)=>{sent=options.headers; return new Response(JSON.stringify({detail}), {status:401});},
    tenantLogout:()=>{logout=true;}, document:{getElementById:()=>({style:{}})}, showAuthView:()=>{}, tenantMessage:()=>{}};
  vm.runInNewContext(wrapper, context);
  await context.window.fetch('https://app.test/app/product/123');
  assert.equal(sent.get('Authorization'), 'Bearer account-token');
  assert.equal(sent.get('X-Shopify-Session-Token'), 'shopify-token');
  assert.equal(sent.get('X-Shop-ID'), '7');
  assert.equal(logout, expectedLogout);
}
(async()=>{
  await check('Connect an AliExpress account first', false);
  await check('Invalid or expired Shopify session', false);
  await check('Invalid or expired AliSync session', true);
  console.log('Authentication request regression checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
