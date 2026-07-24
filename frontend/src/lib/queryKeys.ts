export const queryKeys = {
  leads: ['leads'] as const,
  emails: ['emails'] as const,
  documents: ['documents'] as const,
  chatSessions: ['chat-sessions'] as const,
  chatHistory: (sessionId: string) => ['chat-history', sessionId] as const,
};
