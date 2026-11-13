// The host and portable CLI share one stable default independently of program installation paths.
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

export function defaultWorkspace(env = process.env, platform = process.platform, home = homedir()) {
  return resolve(env.AUTOSKILL_WORKSPACE || join(platform === 'win32' ? env.USERPROFILE || home : home, 'autoSkill-data'));
}

export function workspaceFor(spec) {
  return spec.workspace ?? spec.env?.AUTOSKILL_WORKSPACE ?? defaultWorkspace();
}
