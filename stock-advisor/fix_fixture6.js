const fs = require('fs');
let content = fs.readFileSync('_t_log_ui_test.mjs', 'utf8');

const lines = content.split('\n');
const fixture = lines[113];

console.log('Fixture line 114:');
console.log('  Display:', fixture);
console.log('  Bytes:', Buffer.from(fixture).toString('hex'));

// The fixture now has < (bytes 26 6c 74 3b), but esc() converts & to &
// So it becomes &lt; in the output
// We need the fixture to have literal < (byte 3c) so esc() converts it to < once
const fixed = fixture.replace('<', '<');
console.log('Fixed fixture:');
console.log('  Display:', fixed);
console.log('  Bytes:', Buffer.from(fixed).toString('hex'));

lines[113] = fixed;
content = lines.join('\n');
fs.writeFileSync('_t_log_ui_test.mjs', content);
console.log('Fixed fixture - back to literal <');