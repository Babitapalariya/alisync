const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = vm.createContext({activeShopId:1,URL, setInterval:()=>{}, escapeHtml:value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))});
vm.runInContext(fs.readFileSync(__dirname+'/orders.js','utf8'),context);
const render = overrides => vm.runInContext(`openedOrder={currency:'USD',cancelled:false};renderOrderLine(${JSON.stringify({id:1,title:'<script>alert(1)</script>',variant:'Black',quantity:2,revenue:'59.98',status:'uncertain',product_id:'123',sku_id:'456',supplier_order_id:null,quote:null,tracking:[],fulfillment_state:null,...overrides})})`,context);
let html=render({});
assert.ok(html.includes('Link existing supplier order'),'Uncertain purchases must offer recovery');
assert.ok(!html.includes('<script>'),'Product names must be escaped');
assert.ok(!html.includes('Buy on AliExpress'),'Uncertain purchases must not be retried');
html=render({status:'awaiting_payment',supplier_order_id:'12345',orders_url:'https://www.aliexpress.com/p/order/index.html',payment_confirmed:false,supplier_total:'24.00',supplier_currency:'USD'});
assert.ok(html.includes('Open AliExpress orders'));
assert.ok(html.includes('did not provide a direct payment URL'));
assert.ok(html.includes('Not confirmed'));
assert.ok(html.includes('Verified supplier total: USD 24.00'));
html=render({status:'awaiting_payment',supplier_order_id:'12345',payment_url:'https://www.aliexpress.com/pay'});
assert.ok(html.includes('Continue payment'));
assert.ok(!render({supplier_order_id:'12345',payment_url:'https://aliexpress.com.evil.test/pay'}).includes('href="https://aliexpress.com.evil.test'));
assert.ok(!render({status:'creating'}).includes('Link existing supplier order'));
console.log('Order rendering, recovery, escaping and payment-link checks passed.');

html=render({product_id:null,supplier_product_url:"https://www.aliexpress.com/item/123456.html",status:"new"});
assert.ok(html.includes("Open in AliExpress"));
assert.ok(html.includes('href="https://www.aliexpress.com/item/123456.html"'));

// Production deployment must work even when the host serves only index.html.
const deployedHtml=fs.readFileSync(__dirname+'/index.html','utf8');
for(const [ext,tag] of [['css','style'],['js','script']]){
  const bundle=deployedHtml.match(new RegExp(`<${tag} id="orders-${ext}-bundle">\\r?\\n([\\s\\S]*?)\\r?\\n</${tag}>`));
  assert.ok(bundle, `Missing embedded Orders ${ext}`);
  assert.equal(bundle[1].replace(/\r\n/g,'\n'),fs.readFileSync(__dirname+`/orders.${ext}`,'utf8').trim().replace(/\r\n/g,'\n'), 'Run node frontend/build_orders.cjs after changing order assets');
}
assert.ok(!deployedHtml.includes('src="/orders.js"'));
assert.ok(!deployedHtml.includes('href="/orders.css"'));
console.log('Single-file deployment checks passed.');

vm.runInContext("rememberOrderOrigin(1,'US')",context);
assert.ok(render({status:'failed'}).includes('<option selected>US</option>'),'Failed quote must retain chosen origin');
context.activeShopId=2;
assert.ok(render({status:'new'}).includes('<option selected>CN</option>'),'Origin preference must stay store scoped');
context.activeShopId=1;
context.document={getElementById:id=>({value:id.startsWith('order-origin')?'US':'OLD_CN_SERVICE'})};
vm.runInContext("openedOrder={lines:[{id:1,quote:{ship_from:'CN'}}]}",context);
const request=vm.runInContext('orderQuoteRequest(1)',context);
assert.equal(request.ship_from,'US');
assert.equal(request.shipping_code,undefined,'Do not reuse shipping service from another origin');
console.log('Ship-from selection and retry checks passed.');

const savedOrigins=new Map();
context.sessionStorage={setItem:(k,v)=>savedOrigins.set(k,v),getItem:k=>savedOrigins.get(k)};
vm.runInContext("rememberOrderOrigin(1,'US');orderOrigins.clear()",context);
assert.equal(vm.runInContext('savedOrderOrigin(1)',context),'US','Restore warehouse after page state resets');
console.log('Warehouse persistence check passed.');

vm.runInContext("rememberOrderOrigin(1,'CN')",context);
html=render({status:'failed',error:'Selected Ships from: US. This variant ships from CN. Select CN and retry.'});
assert.ok(html.includes('Previous quote used US. Ships from is now CN'));
assert.ok(!html.includes('Selected Ships from: US.'));
context.document={getElementById:id=>({value:id==='order-origin-1'?'CN':id==='order-origin-2'?'US':''})};
vm.runInContext('openedOrder={lines:[{id:1},{id:2}]}',context);
assert.equal(vm.runInContext('orderQuoteRequest(1).ship_from',context),'CN');
assert.equal(vm.runInContext('orderQuoteRequest(2).ship_from',context),'US');
console.log('Per-item origins and outdated warning checks passed.');

