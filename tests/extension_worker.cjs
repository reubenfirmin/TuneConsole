// Execute the real classic worker and its importScripts dependency graph in one VM realm.
// Chrome/page mocks remain explicit in each behavioral case.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../extension');

exports.loadWorker = (globals, extensionRoot = root) => {
  const context = vm.isContext(globals) ? globals : vm.createContext(globals);
  context.importScripts = (...files) => {
    for (const file of files) {
      vm.runInContext(fs.readFileSync(path.join(extensionRoot, file), 'utf8'), context, {filename: file});
    }
  };
  vm.runInContext(fs.readFileSync(path.join(extensionRoot, 'background.js'), 'utf8'), context,
    {filename: 'background.js'});
  return vm.runInContext('tuneConsole', context);
};
