// The shared panel's archive controls operate on the browser preview workspace.
import { render as renderMaterials } from '../skills/autoskill/app/ui/js/pages/project-materials.js?live';

export async function render(view, options) {
  await renderMaterials(view, options);
  const directory = view.querySelector('.card-body > code');
  if (directory) {
    directory.previousElementSibling.textContent = '示例项目目录';
    directory.textContent = 'projects/' + directory.textContent.split('/').at(-1);
  }
  const formats = view.querySelector('#share-format');
  if (!formats) return;
  formats.options[0].textContent = '预览项目分享包';
  formats.options[1].textContent = '预览项目和执行历史';
  formats.closest('section').querySelector('p.sub').textContent = '将当前项目下载为 ZIP，可在其他浏览器打开本预览后导入。';
  const include = view.querySelector('#share-secrets');
  const passphrase = view.querySelector('#share-passphrase');
  include.parentElement.lastChild.textContent = '携带当前示例项目引用的凭据（加密）';
  view.querySelector('#share-overwrite').parentElement.lastChild.textContent = '替换当前浏览器内的同名项目';
  formats.addEventListener('change', () => {
    include.disabled = false;
    passphrase.disabled = false;
  });
  const path = view.querySelector('#share-source');
  path.placeholder = '本次导出的分享包文件名';
  const field = path.closest('.field');
  field.querySelector('label').textContent = '本次导出的文件名';
  field.querySelector('.hint').textContent = '也可直接选择已下载的预览 ZIP 文件。';
  const summary = field.closest('details')?.querySelector('summary');
  if (summary) summary.textContent = '使用本次导出的分享包';
}
