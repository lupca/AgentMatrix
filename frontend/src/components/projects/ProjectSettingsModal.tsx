import React, { useState, useEffect } from 'react';
import { Project } from '../../types/project';
import { X, Save, AlertCircle } from 'lucide-react';

interface ProjectSettingsModalProps {
  project: Project;
  isOpen: boolean;
  onClose: () => void;
  onSave: (updatedProject: Partial<Project>) => Promise<void>;
}

export const ProjectSettingsModal: React.FC<ProjectSettingsModalProps> = ({ project, isOpen, onClose, onSave }) => {
  const [formData, setFormData] = useState<Partial<Project>>({});
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      setFormData({
        name: project.name,
        description: project.description || '',
        status: project.status,
        context_md: project.context_md || '',
        repo_root: project.repo_root || '',
        task_prefix: project.task_prefix || '',
        autonomy_policy: project.autonomy_policy || null,
      });
      setError(null);
    }
  }, [isOpen, project]);

  if (!isOpen) return null;

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handlePolicyChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    try {
      const parsed = e.target.value ? JSON.parse(e.target.value) : null;
      setFormData((prev) => ({ ...prev, autonomy_policy: parsed }));
    } catch (err) {
      // Just keep string if it's invalid JSON, we'll validate on save or let user fix it
      setFormData((prev) => ({ ...prev, autonomy_policy: e.target.value as any }));
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);
    setError(null);

    // Validate JSON if policy is string
    if (typeof formData.autonomy_policy === 'string' && formData.autonomy_policy.trim() !== '') {
      try {
        formData.autonomy_policy = JSON.parse(formData.autonomy_policy);
      } catch (err) {
        setError("Autonomy policy must be valid JSON.");
        setIsSaving(false);
        return;
      }
    }

    try {
      await onSave(formData);
      onClose();
    } catch (err: any) {
      setError(err.message || "Failed to save project settings");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
      <div className="bg-gray-900 border border-gray-800 rounded-2xl w-full max-w-2xl max-h-[90vh] overflow-hidden flex flex-col shadow-2xl">
        <div className="flex items-center justify-between p-5 border-b border-gray-800 bg-gray-950/50">
          <h2 className="text-lg font-semibold text-gray-100">Project Settings</h2>
          <button onClick={onClose} className="p-1.5 text-gray-400 hover:text-gray-100 hover:bg-gray-800 rounded-lg transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-6">
          {error && (
            <div className="mb-6 p-4 rounded-xl bg-red-500/10 border border-red-500/30 text-red-400 text-sm flex items-center gap-2">
              <AlertCircle className="w-5 h-5 flex-shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <form id="project-settings-form" onSubmit={handleSubmit} className="space-y-5">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-gray-300">Project Name <span className="text-red-400">*</span></label>
                <input
                  type="text"
                  name="name"
                  required
                  value={formData.name || ''}
                  onChange={handleChange}
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
                />
              </div>
              
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-gray-300">Status</label>
                <select
                  name="status"
                  value={formData.status || 'active'}
                  onChange={handleChange}
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
                >
                  <option value="active">Active</option>
                  <option value="paused">Paused</option>
                  <option value="completed">Completed</option>
                  <option value="archived">Archived</option>
                </select>
              </div>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium text-gray-300">Description</label>
              <input
                type="text"
                name="description"
                value={formData.description || ''}
                onChange={handleChange}
                className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium text-gray-300">Context Markdown</label>
              <textarea
                name="context_md"
                rows={4}
                value={formData.context_md || ''}
                onChange={handleChange}
                placeholder="Global context instructions for agents working on this project..."
                className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 font-mono focus:outline-none focus:border-indigo-500"
              />
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-gray-300">Repository Root</label>
                <input
                  type="text"
                  name="repo_root"
                  value={formData.repo_root || ''}
                  onChange={handleChange}
                  placeholder="/path/to/repo"
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 font-mono focus:outline-none focus:border-indigo-500"
                />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-gray-300">Task Prefix</label>
                <input
                  type="text"
                  name="task_prefix"
                  value={formData.task_prefix || ''}
                  onChange={handleChange}
                  placeholder="e.g. PMI"
                  className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 font-mono focus:outline-none focus:border-indigo-500"
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium text-gray-300">Autonomy Policy (JSON)</label>
              <textarea
                name="autonomy_policy"
                rows={3}
                value={typeof formData.autonomy_policy === 'string' ? formData.autonomy_policy : JSON.stringify(formData.autonomy_policy || {}, null, 2)}
                onChange={handlePolicyChange}
                placeholder="{}"
                className="w-full bg-gray-950 border border-gray-800 rounded-lg px-3 py-2 text-sm text-gray-200 font-mono focus:outline-none focus:border-indigo-500"
              />
            </div>
          </form>
        </div>

        <div className="p-5 border-t border-gray-800 bg-gray-950/50 flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-gray-300 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors"
          >
            Cancel
          </button>
          <button
            type="submit"
            form="project-settings-form"
            disabled={isSaving}
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-500 rounded-lg transition-colors disabled:opacity-50"
          >
            {isSaving ? (
              <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            Save Changes
          </button>
        </div>
      </div>
    </div>
  );
};
