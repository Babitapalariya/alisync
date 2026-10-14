const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(__dirname + '/index.html', 'utf8');
const context = vm.createContext({});
for (const name of ['formatIncrease', 'getDraftVariantIncrease', 'addVariantPriceIncrease', 'refreshManualIncrease', 'increaseVariantPrice', 'increaseAllVariants', 'increaseMappingVariantPrice', 'increaseAllMappingVariants']) {
  const source = html.match(new RegExp('(?:  )?function ' + name + '\\([^]*?\\n(?:  )?}'));
  assert.ok(source, name);
  vm.runInContext(source[0], context);
}
function input(price = 10, saved = 0, product = 0) {
  return {value: String(price), dataset: {originalPrice: String(price), priceIncrease: String(saved), productPriceIncrease: String(product)}, closest: () => ({querySelector: () => null})};
}
context.showToast = () => {};
for (const name of ['increaseVariantPrice', 'increaseMappingVariantPrice', 'increaseAllVariants', 'increaseAllMappingVariants']) {
  for (const row of [input(), input(15, 5), input(15, 0, 5)]) {
    context.document = {querySelectorAll: () => [row]};
    const button = {closest: () => ({querySelector: () => row})};
    context.prompt = () => '5';
    context[name](button, '1');
    assert.equal(row.value, '15.00', name);
    context.prompt = () => '3';
    context[name](button, '1');
    assert.equal(row.value, '13.00', name);
    assert.equal(context.getDraftVariantIncrease(row) + Number(row.dataset.productPriceIncrease), 3);
    // Saving and reopening must preserve the same replacement behavior.
    row.dataset.originalPrice = row.value;
    row.dataset.priceIncrease = row.dataset.pendingIncrease;
    delete row.dataset.pendingIncrease;
    context[name](button, '1');
    assert.equal(row.value, '13.00', name);
  }
}
for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(script[1]);
console.log('Both edit forms: single/all, repeated, saved, and product markup replacement passed.');
