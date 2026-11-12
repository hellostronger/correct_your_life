const fs = require('fs');
let content = fs.readFileSync('_t_log_ui_test.mjs', 'utf8');

const lines = content.split('\n');

// Check test 4 - lines 221-271
for (let i = 220; i < 275; i++) {
  if (lines[i].trim()) {
    console.log(i+1, ':', lines[i].substring(0, 100));
  }
}