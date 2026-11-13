// DSH-specific adapters share the portable application's existing HTTP service.
import { HostService } from './host-service.mjs';
import { registerTools } from './host-tools.mjs';
import { attachContext } from './host-context.mjs';
import { registerCommands } from './host-commands.mjs';
import { registerRoutes } from './host-routes.mjs';

export function attachHostBackend(ctx, spec, options = {}) {
  const service = options.service ?? new HostService(spec, options);
  if (typeof ctx.inject !== 'function') return service;
  ctx.inject(['fs', 'workingDirectory'], (host) => {
    const fs = host.fs;
    const workingDirectory = host.workingDirectory;
    service.fs = fs;
    service.workingDirectory = workingDirectory;
    host.effect(() => () => {
      if (service.fs === fs) service.fs = undefined;
      if (service.workingDirectory === workingDirectory) service.workingDirectory = undefined;
    }, 'autoskill: host filesystem');
  });
  ctx.inject(['tools'], (host) => registerTools(host, service));
  ctx.inject(['agents'], (host) => attachContext(host, service));
  ctx.inject(['commands'], (host) => registerCommands(host, service));
  ctx.inject(['connection'], (host) => registerRoutes(host, service));
  return service;
}
