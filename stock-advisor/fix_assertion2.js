const fs = require('fs');
let content = fs.readFileSync('_t_log_ui_test.mjs', 'utf8');

const lines = content.split('\n');
const line = lines[215];

console.log('Current assertion:');
console.log('  Display:', line);
console.log('  Bytes:', Buffer.from(line).toString('hex'));

// The innerHTML contains < (HTML entity for <) which is bytes 26 6c 74 3b
// The assertion should check for < (the literal 4 characters: & l t ;)
// Currently it checks for literal < (byte 3c) - that's wrong

// Build the correct assertion with the HTML entity
const ltEntity = Buffer.from([0x26, 0x6c, 0x74, 0x3b]).toString('utf8');  // '<'
const correct = "includes($('t-log-hist-body').innerHTML, '偏离 0.5% " + ltEntity + " 需 1.7%', '历史 hold 原因也在（已转义为 <）');";
console.log('Correct:');
console.log('  Display:', correct);
console.log('  Bytes:', Buffer.from(correct).toString('hex'));

lines[215] = correct;
content = lines.join('\n');
fs.writeFileSync('_t_log_ui_test.mjs', content);
console.log('Fixed assertion');