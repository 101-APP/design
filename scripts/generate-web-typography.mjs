import fs from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';

const collectStyles = (node, parts = [], result = []) => {
  if (!node || typeof node !== 'object') return result;
  if (node.$type === 'textStyle') result.push({ name: `text-${parts.join('-')}`, value: node.value });
  else for (const [key, value] of Object.entries(node)) collectStyles(value, [...parts, key], result);
  return result;
};

export const renderWebTypography = (source) => {
  const groups = new Map();
  for (const { name, value } of collectStyles(source.styles.text.web)) {
    const properties = {
      'font-family': `"${value.fontFamily}", sans-serif`,
      'font-size': `${value.fontSize}px`,
      'font-weight': String(value.fontWeight),
      'line-height': 'normal',
      'letter-spacing': 'calc(1em * 0 / 100)',
      'text-decoration-line': 'none',
    };
    if (Object.keys(value.openTypeFeatures ?? {}).length) properties['font-feature-settings'] = Object.entries(value.openTypeFeatures).map(([tag, enabled]) => `"${tag.toLowerCase()}" ${enabled ? 1 : 0}`).join(', ');
    for (const [property, cssValue] of Object.entries(properties)) {
      const key = `${property}: ${cssValue};`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(`.${name}`);
    }
  }
  // Properties with the same selector set share a single rule as well.
  const rules = new Map();
  for (const [declaration, selectors] of groups) {
    const selector = selectors.join(',\n');
    if (!rules.has(selector)) rules.set(selector, []);
    rules.get(selector).push(declaration);
  }
  return [...rules].map(([selector, declarations]) => `${selector} {\n${declarations.map(value => `  ${value}`).join('\n')}\n}`).join('\n\n') + '\n';
};

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const root = new URL('../', import.meta.url);
  const source = JSON.parse(fs.readFileSync(new URL('tokens.json', root), 'utf8'));
  const filename = new URL('web/tokens.css', root);
  const original = fs.readFileSync(filename, 'utf8');
  const start = original.indexOf('.text-');
  const end = original.indexOf('.shadow', start);
  if (start < 0 || end < 0) throw new Error('Typography section boundaries are missing');
  fs.writeFileSync(filename, original.slice(0, start) + renderWebTypography(source) + '\n' + original.slice(end));
  console.log(`Generated web typography: ${fileURLToPath(filename)}`);
}
