// Portable alternative to the native 0.6.10 compiler, including on ARM machines.
const fs = require('node:fs')
const path = require('node:path')
const root = path.resolve(__dirname, '..')
let solc
try {
  solc = require(require.resolve('solc', { paths: [path.join(root, 'tools/solcjs')] }))
} catch {
  console.error('Install the local compiler first: npm install --prefix tools/solcjs --no-save --no-audit --no-fund solc@0.6.10')
  process.exit(1)
}
if (!solc.version().startsWith('0.6.10+')) {
  console.error('The local solc-js compiler must be version 0.6.10; found ' + solc.version())
  process.exit(1)
}
const sources = Object.fromEntries(['RootHashRegistry', 'RootHashRegistryV2'].map(name => [
  `contracts/${name}.sol`, { content: fs.readFileSync(path.join(root, `contracts/${name}.sol`), 'utf8') }
]))
const output = JSON.parse(solc.compile(JSON.stringify({
  language: 'Solidity', sources,
  settings: { outputSelection: { '*': { '*': ['abi', 'evm.bytecode.object'] } } }
})))
for (const error of output.errors || []) console.error(error.formattedMessage)
if ((output.errors || []).some(error => error.severity === 'error')) process.exit(1)
const directory = path.join(root, 'contracts/build')
fs.mkdirSync(directory, { recursive: true })
for (const contracts of Object.values(output.contracts)) {
  for (const [name, contract] of Object.entries(contracts)) {
    fs.writeFileSync(path.join(directory, name + '.abi'), JSON.stringify(contract.abi))
    fs.writeFileSync(path.join(directory, name + '.bin'), contract.evm.bytecode.object)
    console.log('Compiled ' + name + ' with ' + solc.version())
  }
}
