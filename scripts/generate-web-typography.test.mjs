import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { renderWebTypography } from './generate-web-typography.mjs';
const source=JSON.parse(fs.readFileSync(new URL('../tokens.json',import.meta.url),'utf8'));
const declarations=(css,name)=>{const values={};for(const match of css.matchAll(/([^{}]+)\{([^{}]+)\}/g)){if(!match[1].trim().split(/,\s*/).includes('.'+name))continue;for(const decl of match[2].matchAll(/([\w-]+):\s*([^;]+);/g))values[decl[1]]=decl[2].trim()}return values};
test('exposes the seven approved sizes and the existing three weights',()=>{
 const css=renderWebTypography(source);
 for(const[group,size]of[['caption',10],['headline',24],['display',48]])for(const[variant,weight]of[['regular',400],['medium',500],['semibold',600]]){
  const style=declarations(css,`text-${group}-${variant}`);assert.equal(style['font-size'],`${size}px`);assert.equal(style['font-weight'],String(weight));assert.equal(style['font-family'],'"Roboto", sans-serif');
 }
 assert.equal(declarations(css,'text-title-3-medium')['font-weight'],'600');
 assert.equal(declarations(css,'text-body-regular')['font-size'],'14px');
 assert.equal((css.match(/font-family:/g)??[]).length,1);
});
