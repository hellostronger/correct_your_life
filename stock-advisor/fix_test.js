const fs = require('fs');
let content = fs.readFileSync('_t_log_ui_test.mjs', 'utf8');

const lines = content.split('\n');
const line = lines[215];

console.log('Current assertion:');
console.log('  Bytes:', Buffer.from(line).toString('hex'));

// The innerHTML contains < (HTML entity for <)
// In JavaScript source, we need to write the literal characters & l t ; (4 bytes: 26 6c 74 3b)
// to check for the entity in the string

// Let's manually construct the correct assertion with the HTML entity
const correct = "includes($('t-log-hist-body').innerHTML, '偏离 0.5% < 需 1.7%', '历史 hold 原因也在（已转义为 <）');";
console.log('Correct:');
console.log('  Display:', correct);
console.log('  Bytes:', Buffer.from(correct).toString('hex'));

lines[215] = correct;
content = lines.join('\n');
fs.writeFileSync('_t_log_ui_test.mjs', content);
console.log('Fixed');