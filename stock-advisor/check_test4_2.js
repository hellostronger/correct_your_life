const fs = require('fs');
let content = fs.readFileSync('_t_log_ui_test.mjs', 'utf8');

const lines = content.split('\n');

// Check test 4 - add debug logging
for (let i = 260; i < 275; i++) {
  console.log(i+1, ':', lines[i]);
}