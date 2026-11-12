const fs = require('fs');
let content = fs.readFileSync('_t_log_ui_test.mjs', 'utf8');

const lines = content.split('\n');
const fixture = lines[113];

console.log('Fixture line 114:');
console.log('  Display:', fixture);
console.log('  Bytes:', Buffer.from(fixture).toString('hex'));

// The fixture has literal < (byte 3c), but it should have the HTML entity < (bytes 26 6c 74 3b)
// because the template renders it through esc() which converts < to <
// So we need to replace the literal < character (3c) with the 4-character entity <

const fixed = fixture.replace(String.fromCharCode(0x3c), '<');
console.log('Fixed fixture:');
console.log('  Display:', fixed);
console.log('  Bytes:', Buffer.from(fixed).toString('hex'));

lines[113] = fixed;
content = lines.join('\n');
fs.writeFileSync('_t_log_ui_test.mjs', content);
console.log('Fixed fixture');