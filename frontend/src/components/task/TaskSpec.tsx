import React, { useState } from 'react';
import { Task } from '../../types/task';
import {
  FileText,
  CheckSquare,
  AlertOctagon,
  FileCode,
  FlaskConical,
  GitBranch,
  ShieldCheck,
  Award,
  ChevronDown,
  ChevronUp,
  Terminal,
  Sparkles,
  Info,
} from 'lucide-react';

interface TaskSpecProps {
  task: Task;
}

export const TaskSpec: React.FC<TaskSpecProps> = ({ task }) => {
  const [activeTab, setActiveTab] = useState<'overview' | 'files' | 'logs'>('overview');
  const [checkedCriteria, setCheckedCriteria] = useState<Record<number, boolean>>({});

  const toggleCriteria = (idx: number) => {
    setCheckedCriteria((prev) => ({ ...prev, [idx]: !prev[idx] }));
  };

  const hasFiles = task.files && task.files.length > 0;
  const hasTests = task.tests && task.tests.length > 0;
  const hasFlows = task.flows && task.flows.length > 0;
  const hasCriteria = task.acceptance_criteria && task.acceptance_criteria.length > 0;
  const hasFindings = task.findings && task.findings.length > 0;

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-2xl overflow-hidden shadow-xl backdrop-blur-md space-y-0">
      {/* Navigation Tabs Header */}
      <div className="bg-gray-950/80 px-6 py-3 border-b border-gray-800 flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <button
            onClick={() => setActiveTab('overview')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors flex items-center gap-1.5 ${
              activeTab === 'overview'
                ? 'bg-indigo-600 text-white shadow-md'
                : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'
            }`}
          >
            <FileText className="w-3.5 h-3.5" />
            <span>Task Spec & Plan</span>
          </button>

          <button
            onClick={() => setActiveTab('files')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors flex items-center gap-1.5 ${
              activeTab === 'files'
                ? 'bg-indigo-600 text-white shadow-md'
                : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'
            }`}
          >
            <FileCode className="w-3.5 h-3.5" />
            <span>Artifacts & Files ({(task.files?.length || 0) + (task.tests?.length || 0)})</span>
          </button>

          {task.error && (
            <button
              onClick={() => setActiveTab('logs')}
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors flex items-center gap-1.5 ${
                activeTab === 'logs'
                  ? 'bg-red-600 text-white shadow-md'
                  : 'text-red-400 hover:bg-red-950/40'
              }`}
            >
              <Terminal className="w-3.5 h-3.5" />
              <span>Execution Error</span>
            </button>
          )}
        </div>
      </div>

      <div className="p-6 space-y-6">
        {/* Awaiting Approval Warning Banner */}
        {task.awaiting_approval && (
          <div className="bg-amber-500/10 border border-amber-500/30 rounded-xl p-4 text-amber-200 space-y-2">
            <div className="flex items-center gap-2 font-bold text-amber-400 text-sm">
              <AlertOctagon className="w-4 h-4" />
              <span>Human-in-the-Loop Approval Required</span>
            </div>
            <p className="text-xs text-amber-300/90 leading-relaxed">
              {task.approval_prompt ||
                'This task has reached an interactive gate threshold requiring operator review before proceeding to final execution.'}
            </p>
          </div>
        )}

        {/* Tab 1: Overview & Specification */}
        {activeTab === 'overview' && (
          <div className="space-y-6">
            {/* Raw Input Prompt */}
            {task.raw_input && (
              <div className="space-y-2">
                <h3 className="text-xs font-mono font-bold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                  <Sparkles className="w-3.5 h-3.5 text-indigo-400" />
                  Raw Input / Prompt Specification
                </h3>
                <div className="bg-gray-950/80 border border-gray-800 rounded-xl p-4 font-mono text-xs text-gray-200 leading-relaxed whitespace-pre-wrap">
                  {task.raw_input}
                </div>
              </div>
            )}

            {/* Plan Section */}
            {task.plan ? (
              <div className="space-y-2">
                <h3 className="text-xs font-mono font-bold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                  <FileText className="w-3.5 h-3.5 text-indigo-400" />
                  Execution Plan Breakdown
                </h3>
                <div className="bg-gray-950/80 border border-gray-800 rounded-xl p-4 text-xs text-gray-200 leading-relaxed whitespace-pre-wrap font-sans">
                  {task.plan}
                </div>
              </div>
            ) : (
              <div className="bg-gray-950/40 border border-dashed border-gray-800 rounded-xl p-4 text-center text-xs text-gray-500">
                No formal plan text recorded for this task.
              </div>
            )}

            {/* Acceptance Criteria */}
            <div className="space-y-2">
              <h3 className="text-xs font-mono font-bold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                <CheckSquare className="w-3.5 h-3.5 text-indigo-400" />
                Acceptance Criteria Checklist
              </h3>
              {hasCriteria ? (
                <div className="bg-gray-950/80 border border-gray-800 rounded-xl divide-y divide-gray-800/60 overflow-hidden">
                  {task.acceptance_criteria!.map((item, idx) => {
                    const isChecked = checkedCriteria[idx] ?? false;
                    return (
                      <div
                        key={idx}
                        onClick={() => toggleCriteria(idx)}
                        className={`p-3 text-xs flex items-center space-x-3 cursor-pointer transition-colors ${
                          isChecked ? 'bg-indigo-950/20 text-indigo-200' : 'hover:bg-gray-800/40 text-gray-300'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={isChecked}
                          onChange={() => {}} // handled by parent onClick
                          className="rounded border-gray-700 bg-gray-900 text-indigo-500 focus:ring-0 cursor-pointer"
                        />
                        <span className={`flex-1 ${isChecked ? 'line-through opacity-75' : ''}`}>
                          {item}
                        </span>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="bg-gray-950/40 border border-dashed border-gray-800 rounded-xl p-4 text-center text-xs text-gray-500">
                  No specific acceptance criteria listed.
                </div>
              )}
            </div>

            {/* Verdict & Findings */}
            {(task.verdict || hasFindings || task.result_ref) && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {task.verdict && (
                  <div className="bg-gray-950/80 border border-gray-800 rounded-xl p-4 space-y-1">
                    <div className="text-xs font-semibold text-emerald-400 flex items-center gap-1.5">
                      <Award className="w-4 h-4" />
                      <span>Review Verdict</span>
                    </div>
                    <p className="text-xs text-gray-200 font-medium">{task.verdict}</p>
                  </div>
                )}

                {task.result_ref && (
                  <div className="bg-gray-950/80 border border-gray-800 rounded-xl p-4 space-y-1">
                    <div className="text-xs font-semibold text-indigo-400 flex items-center gap-1.5">
                      <ShieldCheck className="w-4 h-4" />
                      <span>Result Reference</span>
                    </div>
                    <p className="text-xs font-mono text-gray-300 truncate">{task.result_ref}</p>
                  </div>
                )}

                {hasFindings && (
                  <div className="col-span-full bg-gray-950/80 border border-gray-800 rounded-xl p-4 space-y-2">
                    <div className="text-xs font-semibold text-purple-400 flex items-center gap-1.5">
                      <Info className="w-4 h-4" />
                      <span>Key Findings</span>
                    </div>
                    <ul className="list-disc list-inside space-y-1 text-xs text-gray-300">
                      {task.findings!.map((f, i) => (
                        <li key={i}>{f}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Tab 2: Target Files & Test Specs */}
        {activeTab === 'files' && (
          <div className="space-y-6">
            {/* Target Files */}
            <div className="space-y-2">
              <h3 className="text-xs font-mono font-bold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                <FileCode className="w-3.5 h-3.5 text-indigo-400" />
                Target Implementation Files
              </h3>
              {hasFiles ? (
                <div className="bg-gray-950/80 border border-gray-800 rounded-xl p-3 space-y-1.5">
                  {task.files!.map((file, idx) => (
                    <div
                      key={idx}
                      className="px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 font-mono text-xs text-indigo-300 flex items-center justify-between"
                    >
                      <span>{file}</span>
                      <span className="text-[10px] text-gray-500">Source</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-xs text-gray-500 italic p-3">No specific target files specified.</div>
              )}
            </div>

            {/* Test Specs */}
            <div className="space-y-2">
              <h3 className="text-xs font-mono font-bold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                <FlaskConical className="w-3.5 h-3.5 text-emerald-400" />
                Verification Tests
              </h3>
              {hasTests ? (
                <div className="bg-gray-950/80 border border-gray-800 rounded-xl p-3 space-y-1.5">
                  {task.tests!.map((test, idx) => (
                    <div
                      key={idx}
                      className="px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 font-mono text-xs text-emerald-300 flex items-center justify-between"
                    >
                      <span>{test}</span>
                      <span className="text-[10px] text-emerald-500/80">Test Suite</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-xs text-gray-500 italic p-3">No specific test paths attached.</div>
              )}
            </div>

            {/* Workflows / Flows */}
            {hasFlows && (
              <div className="space-y-2">
                <h3 className="text-xs font-mono font-bold uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                  <GitBranch className="w-3.5 h-3.5 text-purple-400" />
                  LangGraph Execution Flows
                </h3>
                <div className="bg-gray-950/80 border border-gray-800 rounded-xl p-3 space-y-1.5">
                  {task.flows!.map((flow, idx) => (
                    <div
                      key={idx}
                      className="px-3 py-2 rounded-lg bg-gray-900 border border-gray-800 font-mono text-xs text-purple-300"
                    >
                      {flow}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Tab 3: Execution Error Logs */}
        {activeTab === 'logs' && task.error && (
          <div className="space-y-3">
            <h3 className="text-xs font-mono font-bold uppercase tracking-wider text-red-400 flex items-center gap-1.5">
              <Terminal className="w-3.5 h-3.5" />
              Runtime Traceback & Error Output
            </h3>
            <div className="bg-red-950/30 border border-red-500/30 rounded-xl p-4 font-mono text-xs text-red-200 overflow-x-auto whitespace-pre-wrap leading-relaxed">
              {task.error}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default TaskSpec;
