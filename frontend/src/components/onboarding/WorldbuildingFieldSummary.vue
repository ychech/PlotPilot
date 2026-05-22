<template>
  <n-collapse v-if="field" class="wb-field-summary" :default-expanded-names="compact ? [] : ['summary']">
    <n-collapse-item name="summary">
      <template #header>
        <span class="wb-field-summary__header">结构化摘要</span>
      </template>
      <div class="wb-field-summary__body">
        <div v-if="keywords.length" class="wb-field-summary__row">
          <span class="wb-field-summary__label">关键词</span>
          <span>{{ keywords.join('、') }}</span>
        </div>
        <div v-if="ladder.length" class="wb-field-summary__row">
          <span class="wb-field-summary__label">结构</span>
          <span>{{ ladder.join(' / ') }}</span>
        </div>
        <div v-if="rules.length" class="wb-field-summary__row">
          <span class="wb-field-summary__label">规则</span>
          <span>{{ rules.join('；') }}</span>
        </div>
        <div v-if="costs.length" class="wb-field-summary__row">
          <span class="wb-field-summary__label">代价</span>
          <span>{{ costs.join('；') }}</span>
        </div>
        <p v-if="field.summary" class="wb-field-summary__summary">{{ field.summary }}</p>
      </div>
    </n-collapse-item>
  </n-collapse>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  listWorldbuildingItems,
  parseStructuredWorldbuildingField,
} from '@/utils/worldbuildingField'

const props = defineProps<{
  value: unknown
  compact?: boolean
}>()

const field = computed(() => parseStructuredWorldbuildingField(props.value))
const keywords = computed(() => listWorldbuildingItems(field.value?.quick_ref?.keywords))
const ladder = computed(() => listWorldbuildingItems(field.value?.quick_ref?.ladder))
const rules = computed(() => listWorldbuildingItems(field.value?.quick_ref?.rules))
const costs = computed(() => listWorldbuildingItems(field.value?.quick_ref?.costs))
</script>

<style scoped>
.wb-field-summary {
  margin-bottom: 6px;
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
  padding: 0 8px;
  background: var(--n-color);
}

.wb-field-summary__header {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-color-2);
}

.wb-field-summary__body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-bottom: 8px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-color-2);
}

.wb-field-summary__row {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr);
  gap: 8px;
}

.wb-field-summary__label {
  color: var(--text-color-3);
}

.wb-field-summary__summary {
  margin: 2px 0 0;
  color: var(--text-color-2);
  white-space: pre-wrap;
}
</style>
