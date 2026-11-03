/*
 * Copyright (c) 2025 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#define USING_LOG_PREFIX SQL_ENG
#include "ob_expr_ai_embed.h"
#include "query/ai/ob_ai_endpoint_resolver.h"
#include "share/rc/ob_server_runtime.h"

using namespace oceanbase::common;
using namespace oceanbase::sql;

namespace oceanbase 
{
namespace sql 
{
ObExprAIEmbed::ObExprAIEmbed(common::ObIAllocator &alloc)
    : ObFuncExprOperator(alloc, 
                        T_FUN_SYS_AI_EMBED, 
                        N_AI_EMBED, 
                        MORE_THAN_ZERO,
                        NOT_VALID_FOR_GENERATED_COL, 
                        NOT_ROW_DIMENSION) 
{
}

ObExprAIEmbed::~ObExprAIEmbed() 
{
}

int ObExprAIEmbed::calc_result_typeN(ObExprResType &type,
                                     ObExprResType *types_stack,
                                     int64_t param_num,
                                     common::ObExprTypeCtx &type_ctx) const 
{
  UNUSED(type_ctx);
  UNUSED(types_stack);
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(param_num > 3 || param_num < 2)) {
    ObString func_name_(get_name());
    ret = OB_ERR_PARAM_SIZE;
    LOG_USER_ERROR(OB_ERR_PARAM_SIZE, func_name_.length(), func_name_.ptr());
  } else {
    types_stack[MODEL_IDX].set_calc_type(ObVarcharType);
    types_stack[MODEL_IDX].set_calc_collation_type(CS_TYPE_UTF8MB4_BIN);
    if (ob_is_text_tc(types_stack[CONTENT_IDX].get_type()) &&
        CS_TYPE_BINARY != types_stack[CONTENT_IDX].get_collation_type()) {
      types_stack[CONTENT_IDX].set_calc_collation_type(CS_TYPE_UTF8MB4_BIN);
    } else {
      types_stack[CONTENT_IDX].set_calc_type(ObVarcharType);
      types_stack[CONTENT_IDX].set_calc_collation_type(CS_TYPE_UTF8MB4_BIN);
    }
    if (param_num == 3) {
      if (ob_is_integer_type(types_stack[DIM_IDX].get_type())) {
        types_stack[DIM_IDX].set_calc_type(ObIntType);
        types_stack[DIM_IDX].set_precision(10);
        types_stack[DIM_IDX].set_scale(0);
      } else {
        ret = OB_INVALID_ARGUMENT;
        LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, dimension parameter must be an integer, not a decimal or float");
      }
    }
    type.set_varchar();
    type.set_collation_type(CS_TYPE_UTF8MB4_BIN);
    type.set_collation_level(CS_LEVEL_COERCIBLE);
  }
  return ret;
}

int ObExprAIEmbed::eval_ai_embed(const ObExpr &expr, ObEvalCtx &ctx, ObDatum &res) 
{
  INIT_SUCC(ret);
  ObDatum *arg_model_id = nullptr;
  ObDatum *arg_content = nullptr;
  ObDatum *arg_dim = nullptr;
  if (expr.arg_cnt_ == 3 ? OB_FAIL(expr.eval_param_value(ctx, arg_model_id, arg_content, arg_dim))
                         : OB_FAIL(expr.eval_param_value(ctx, arg_model_id, arg_content))) {
  } else if (arg_model_id->is_null() || arg_content->is_null()) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, model id or content is null");
    res.set_null();
  } else {
    ObEvalCtx::TempAllocGuard tmp_alloc_g(ctx);
    
    MultimodeAlloctor temp_allocator(tmp_alloc_g.get_allocator());
    lib::ObMallocHookAttrGuard malloc_guard(lib::ObMemAttr(N_AI_EMBED));
    ObAIFuncExprInfo *info = nullptr;
    share::ObAiModelEndpointInfo resolved_endpoint;
    const share::ObAiModelEndpointInfo *endpoint_info = &resolved_endpoint;
    query::ObIAiEndpointResolver *endpoint_resolver =
        ::oceanbase::share::server_service<::oceanbase::query::ObIAiEndpointResolver>();
    ObString model_id = arg_model_id->get_string();
    ObString content;
    if (ob_is_text_tc(expr.args_[CONTENT_IDX]->datum_meta_.type_)) {
      if (OB_FAIL(ObTextStringHelper::read_real_string_data(ctx.exec_ctx_, temp_allocator,
                                                             *arg_content, expr.args_[CONTENT_IDX]->datum_meta_,
                                                             expr.args_[CONTENT_IDX]->obj_meta_.has_lob_header(), content))) {
      }
    } else {
      content = arg_content->get_string();
    }
    if (OB_FAIL(ret)) {
    } else if (model_id.empty() || content.empty()) {
      ret = OB_INVALID_ARGUMENT;
      LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, model id or input is empty");
      res.set_null();
    }
    int64_t dim = 0;
    ObJsonInt *dim_json = nullptr;
    ObJsonObject *config = nullptr;
    if (OB_FAIL(ret)) {
    } else if (OB_NOT_NULL(arg_dim)) {
      if (arg_dim->is_null()) {
        ret = OB_INVALID_ARGUMENT;
        LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, dimension parameter must be a positive integer");
        res.set_null();
      } else {
        dim = arg_dim->get_int();
        if (dim <= 0) {
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, dimension parameter must be a positive integer");
          res.set_null();
        } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_object(temp_allocator, config))) {
        } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_int(temp_allocator, dim, dim_json))) {
        } else if (OB_FAIL(config->add("dimensions", dim_json))) {
        }
      }
    }
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(ObAIFuncUtils::get_ai_func_info(temp_allocator, model_id, info))) {
    } else if (OB_ISNULL(endpoint_resolver)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(endpoint_resolver->resolve_by_model_name(
                   model_id, temp_allocator, resolved_endpoint))) {
    } else {
      ObAIFuncModel model(temp_allocator, *info, *endpoint_info);
      ObString result;
      if (OB_FAIL(model.call_dense_embedding(content, config, result))) {
      } else if (OB_FAIL(ObAIFuncUtils::set_string_result(expr, ctx, res, result))) {
      }
    }
  }
  return ret;
}

int ObExprAIEmbed::eval_batch_ai_embed(const ObExpr &expr,
                                       ObEvalCtx &ctx,
                                       const ObBitVector &skip,
                                       const int64_t size)
{
  int ret = OB_SUCCESS;
  ObDatumVector res_datum = expr.locate_expr_datumvector(ctx);
  ObBitVector &eval_flags = expr.get_evaluated_flags(ctx);
  // provider objects and request buffers live in the operator-level result allocator,
  // and every final string is copied into the result frame below
  ObIAllocator &allocator = ctx.get_expr_res_alloc();
  if (OB_FAIL(expr.args_[MODEL_IDX]->eval_batch(ctx, skip, size))) {
  } else if (OB_FAIL(expr.args_[CONTENT_IDX]->eval_batch(ctx, skip, size))) {
  } else if (expr.arg_cnt_ > 2 && OB_FAIL(expr.args_[DIM_IDX]->eval_batch(ctx, skip, size))) {
  } else {
    ObDatumVector model_datum = expr.args_[MODEL_IDX]->locate_expr_datumvector(ctx);
    ObDatumVector content_datum = expr.args_[CONTENT_IDX]->locate_expr_datumvector(ctx);
    ObDatumVector dim_datum = expr.arg_cnt_ > 2 ? expr.args_[DIM_IDX]->locate_expr_datumvector(ctx)
                                                : ObDatumVector();
    int64_t row = 0;
    while (OB_SUCC(ret) && row < size) {
      if (skip.at(row) || eval_flags.at(row)) {
        ++row;
        continue;
      }
      ObString group_model_id;
      int64_t group_dim = 0;
      bool group_has_dim = false;
      ObArray<int64_t> group_rows;
      ObArray<ObString> group_contents;
      if (model_datum.at(row)->is_null()) {
        // null model id is propagated as a null result, same as null content
        res_datum.at(row)->set_null();
        eval_flags.set(row);
        ++row;
        continue;
      }
      group_model_id = model_datum.at(row)->get_string();
      if (OB_FAIL(ret)) {
      } else if (group_model_id.empty()) {
        ret = OB_INVALID_ARGUMENT;
        LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, model id is empty");
      } else if (expr.arg_cnt_ > 2) {
        ObDatum *dim = dim_datum.at(row);
        if (dim->is_null()) {
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, dimension parameter must be a positive integer");
        } else {
          group_dim = dim->get_int();
          group_has_dim = true;
          if (group_dim <= 0) {
            ret = OB_INVALID_ARGUMENT;
            LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, dimension parameter must be a positive integer");
          }
        }
      }

      // collect a batch of rows sharing the same model id and dimension
      while (OB_SUCC(ret) && row < size && group_rows.count() < BATCH_SIZE) {
        if (skip.at(row) || eval_flags.at(row)) {
          ++row;
          continue;
        }
        ObDatum *model = model_datum.at(row);
        ObDatum *content = content_datum.at(row);
        if (model->is_null() || content->is_null()) {
          // null content is propagated to the result and never sent to the model
          res_datum.at(row)->set_null();
          eval_flags.set(row);
          ++row;
          continue;
        }
        ObString model_id = model->get_string();
        if (model_id.empty()) {
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, model id is empty");
        } else if (model_id.compare(group_model_id) != 0) {
          // a different model starts a new batch
          break;
        } else if (expr.arg_cnt_ > 2) {
          ObDatum *dim = dim_datum.at(row);
          if (dim->is_null() || dim->get_int() != group_dim) {
            if (dim->is_null() || dim->get_int() <= 0) {
              ret = OB_INVALID_ARGUMENT;
              LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, dimension parameter must be a positive integer");
            } else {
              break;
            }
          }
        }
        if (OB_FAIL(ret)) {
        } else {
          ObString content_str;
          if (ob_is_text_tc(expr.args_[CONTENT_IDX]->datum_meta_.type_)) {
            if (OB_FAIL(ObTextStringHelper::read_real_string_data(
                    ctx.exec_ctx_, allocator, *content,
                    expr.args_[CONTENT_IDX]->datum_meta_,
                    expr.args_[CONTENT_IDX]->obj_meta_.has_lob_header(), content_str))) {
            }
          } else {
            content_str = content->get_string();
          }
          if (OB_FAIL(ret)) {
          } else if (content_str.empty()) {
            ret = OB_INVALID_ARGUMENT;
            LOG_USER_ERROR(OB_INVALID_ARGUMENT, "ai_embed, input is empty");
          } else if (OB_FAIL(group_rows.push_back(row))) {
          } else if (OB_FAIL(group_contents.push_back(content_str))) {
          } else {
            ++row;
          }
        }
      }

      if (OB_FAIL(ret) || group_rows.empty()) {
      } else {
        ObJsonObject *config = nullptr;
        ObJsonInt *dim_json = nullptr;
        if (group_has_dim) {
          if (OB_FAIL(ObAIFuncJsonUtils::get_json_object(allocator, config))) {
          } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_int(allocator, group_dim, dim_json))) {
          } else if (OB_FAIL(config->add("dimensions", dim_json))) {
          }
        }
        ObAIFuncExprInfo *info = nullptr;
        share::ObAiModelEndpointInfo resolved_endpoint;
        query::ObIAiEndpointResolver *endpoint_resolver =
            ::oceanbase::share::server_service<::oceanbase::query::ObIAiEndpointResolver>();
        if (OB_FAIL(ret)) {
        } else if (OB_FAIL(ObAIFuncUtils::get_ai_func_info(allocator, group_model_id, info))) {
        } else if (OB_ISNULL(endpoint_resolver)) {
          ret = OB_ERR_UNEXPECTED;
        } else if (OB_FAIL(endpoint_resolver->resolve_by_model_name(
                       group_model_id, allocator, resolved_endpoint))) {
        } else {
          ObAIFuncModel model(allocator, *info, resolved_endpoint);
          ObArray<ObString> results;
          if (OB_FAIL(model.call_dense_embedding_vector_v2(group_contents, config, results))) {
          } else if (results.count() != group_rows.count()) {
            ret = OB_ERR_UNEXPECTED;
          } else {
            for (int64_t i = 0; OB_SUCC(ret) && i < group_rows.count(); ++i) {
              const int64_t result_row = group_rows.at(i);
              const ObString &result_str = results.at(i);
              char *result_buf = expr.alloc_str_res_mem(ctx, result_str.length(), result_row);
              if (OB_ISNULL(result_buf)) {
                ret = OB_ALLOCATE_MEMORY_FAILED;
                LOG_WARN("failed to allocate result buffer", K(ret), K(result_row));
              } else {
                MEMCPY(result_buf, result_str.ptr(), result_str.length());
                res_datum.at(result_row)->set_string(result_buf, result_str.length());
                eval_flags.set(result_row);
              }
            }
          }
        }
      }
    }
  }
  return ret;
}

int ObExprAIEmbed::cg_expr(ObExprCGCtx &expr_cg_ctx, 
                           const ObRawExpr &raw_expr,
                           ObExpr &rt_expr) const 
{
  int ret = OB_SUCCESS;
  // TODO: support schema version match in plan cache for ai func
  // const ObRawExpr *model_key = raw_expr.get_param_expr(0);
  // if (OB_NOT_NULL(model_key)
  //     && (model_key->is_static_scalar_const_expr() || model_key->is_const_expr())
  //     && model_key->get_expr_type() != T_OP_GET_USER_VAR &&
  //     OB_NOT_NULL(expr_cg_ctx.schema_guard_)) {
  //   ObIAllocator *allocator = expr_cg_ctx.allocator_;
  //   ObExecContext *exec_ctx = expr_cg_ctx.session_->get_cur_exec_ctx();
  //   ObObj const_data;
  //   bool got_data = false;
  //   ObAIFuncExprInfo *info = nullptr;
  //   if (OB_ISNULL(allocator)) {
  //     ret = OB_ERR_UNEXPECTED;
  //     LOG_WARN("allocator is null", K(ret));
  //   } else if (OB_FAIL(ObSQLUtils::calc_const_or_calculable_expr(exec_ctx,
  //                                                         model_key,
  //                                                         const_data,
  //                                                         got_data,
  //                                                         *allocator))) {
  //     LOG_WARN("failed to calc offset expr", K(ret));
  //   } else if (!got_data || const_data.is_null()) {
  //   } else if (OB_FAIL(ObAIFuncUtils::get_ai_func_info(*allocator, const_data.get_string(), *expr_cg_ctx.schema_guard_, info))) {
  //     LOG_WARN("failed to get ai func info", K(ret), K(const_data.get_string()));
  //   } else {
  //     rt_expr.extra_info_ = info;
  //   }
  // }

  if (OB_SUCC(ret)) {
    rt_expr.eval_func_ = ObExprAIEmbed::eval_ai_embed;
    rt_expr.eval_batch_func_ = ObExprAIEmbed::eval_batch_ai_embed;
  }
  return ret;
}

} // namespace sql
} // namespace oceanbase
