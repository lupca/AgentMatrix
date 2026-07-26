import { useCallback, useState } from 'react';
import { api } from '../lib/api';
import {
  CoordinatorModelOption,
  providerForModel,
} from '../components/chat/ModelSelector';

export interface ChatSession {
  id: string;
  selected_model?: string | null;
  selected_provider?: string | null;
  messages?: Array<Record<string, unknown>> | null;
}

export function modelOptionForValue(model: string): CoordinatorModelOption {
  const provider = providerForModel(model);
  return { value: model, label: model, provider };
}

/** Persist the coordinator selection for a session. */
export async function updateSessionModel(
  sessionId: string,
  model: string,
): Promise<ChatSession> {
  if (!sessionId) {
    throw new Error('A session is required before selecting a coordinator model.');
  }

  return api.patch<ChatSession>(`/sessions/${encodeURIComponent(sessionId)}`, {
    selected_model: model,
    selected_provider: providerForModel(model),
  });
}

export function useChat(sessionId?: string) {
  const [isModelSwitching, setIsModelSwitching] = useState(false);

  const persistModel = useCallback(
    async (model: string) => {
      setIsModelSwitching(true);
      try {
        return await updateSessionModel(sessionId || '', model);
      } finally {
        setIsModelSwitching(false);
      }
    },
    [sessionId],
  );

  return { isModelSwitching, updateSessionModel: persistModel };
}

export default useChat;
