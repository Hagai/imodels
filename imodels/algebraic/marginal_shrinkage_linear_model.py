from copy import deepcopy
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator
from sklearn.linear_model import LinearRegression, RidgeCV, Ridge, ElasticNetCV
from sklearn.tree import DecisionTreeRegressor
from sklearn.utils.multiclass import check_classification_targets
from sklearn.utils.validation import check_X_y
from sklearn.utils.validation import _check_sample_weight
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, roc_auc_score
from tqdm import tqdm
from sklearn.preprocessing import StandardScaler

import imodels

from sklearn.base import RegressorMixin, ClassifierMixin


class MarginalShrinkageLinearModel(BaseEstimator):
    """Linear model that shrinks towards the marginal effects of each feature."""

    def __init__(
        self,
        est_marginal_name="ridge",
        est_main_name="ridge",
        marginal_divide_by_d=True,
        marginal_sign_constraint=False,
        alphas=(0.1, 1, 10, 100, 1000, 10000),
        random_state=None,
    ):
        """
        Params
        ------
        est_marginal_name : str
            Name of estimator to use for marginal effects (marginal regression)
            If "None", then assume marginal effects are zero (standard Ridge)
        est_main_name : str
            Name of estimator to use for main effects
            If "None", then assume marginal effects are zero (standard Ridge)
            If "ridge", then use RidgeCV with alphas
            If "NNLS", then use non-negative least squares
        marginal_divide_by_d : bool
            If True, then divide marginal effects by n_features
        marginal_sign_constraint : bool
            If True, then constrain main effects to be same sign as marginal effects
        alphas: Tuple[float]
            Alphas to try for ridge regression (if using ridge estimators)
        random_state : int
            Random seed
        """
        self.random_state = random_state
        self.est_marginal_name = est_marginal_name
        self.est_main_name = est_main_name
        self.marginal_divide_by_d = marginal_divide_by_d
        self.marginal_sign_constraint = marginal_sign_constraint
        if isinstance(alphas, float) or isinstance(alphas, int):
            alphas = [alphas]
        self.alphas = alphas

    def fit(self, X, y, sample_weight=None):
        # checks
        X, y = check_X_y(X, y, accept_sparse=False, multi_output=False)
        sample_weight = _check_sample_weight(sample_weight, X, dtype=None)
        if isinstance(self, ClassifierMixin):
            check_classification_targets(y)
            self.classes_, y = np.unique(y, return_inverse=True)

        # center X and y
        self.scalar_X_ = StandardScaler()
        X = self.scalar_X_.fit_transform(X)

        if isinstance(self, RegressorMixin):
            self.scalar_y_ = StandardScaler()
            y = self.scalar_y_.fit_transform(y.reshape(-1, 1)).squeeze()

        # fit marginal
        self.coef_marginal_ = self._fit_marginal(X, y, sample_weight)

        # constrain main effects to be same sign as marginal effects by flipping sign
        # of X appropriately and refitting with a non-negative least squares
        self.est_main_ = self._get_est_from_name(self.est_main_name)

        if self.marginal_sign_constraint:
            assert self.est_main_name == "NNLS", "must use NNLS for sign constraint"
            coef_signs = np.sign(self.coef_marginal_)
            X = X * coef_signs
            self.est_main_.fit(X, y, sample_weight=sample_weight)
            self.est_main_.coef_ = self.est_main_.coef_ * coef_signs
        elif self.est_main_ is None:
            # fit dummy clf and override coefs
            self.est_main_ = RidgeCV(fit_intercept=False)
            self.est_main_.fit(X[:5], y[:5])
            self.est_main_.coef_ = self.coef_marginal_
        else:
            # fit main estimator
            # predicting residuals is the same as setting a prior over coef_marginal
            # because we do solve change of variables ridge(prior = coef = coef - coef_marginal)
            preds_marginal = X @ self.coef_marginal_
            residuals = y - preds_marginal
            self.est_main_.fit(X, residuals, sample_weight=sample_weight)
            self.est_main_.coef_ = self.est_main_.coef_ + self.coef_marginal_

        return self

    def _fit_marginal(self, X, y, sample_weight):
        # initialize marginal estimator
        est_marginal = self._get_est_from_name(self.est_marginal_name)

        # fit marginal estimator to each feature
        if est_marginal is None:
            coef_marginal_ = np.zeros(X.shape[1])
        else:
            coef_marginal_ = []
            for i in range(X.shape[1]):
                est_marginal.fit(X[:, i].reshape(-1, 1), y, sample_weight=sample_weight)
                coef_marginal_.append(deepcopy(est_marginal.coef_))
            coef_marginal_ = np.vstack(coef_marginal_).squeeze()

        # evenly divide effects among features
        if self.marginal_divide_by_d:
            coef_marginal_ /= X.shape[1]

        return coef_marginal_

    def _get_est_from_name(self, est_name):
        return {
            "ridge": RidgeCV(
                fit_intercept=False,
                alphas=self.alphas,
            ),
            "ols": LinearRegression(
                fit_intercept=False,
            ),
            "NNLS": ElasticNetCV(
                l1_ratio=1e-10,
                fit_intercept=False,
                positive=True,
                alphas=self.alphas,
                max_iter=10000,
            ),
            None: None,
            "None": None,
        }[est_name]

    def predict_proba(self, X):
        X = self.scalar_X_.transform(X)
        return self.est_main_.predict_proba(X)

    def predict(self, X):
        X = self.scalar_X_.transform(X)
        pred = self.est_main_.predict(X)
        return self.scalar_y_.inverse_transform(pred.reshape(-1, 1)).squeeze()


class MarginalShrinkageLinearModelRegressor(
    MarginalShrinkageLinearModel, RegressorMixin
):
    ...


# class MarginalShrinkageLinearModelClassifier(
#     MarginalShrinkageLinearModel, ClassifierMixin
# ):
#     ...


if __name__ == "__main__":
    # X, y, feature_names = imodels.get_clean_dataset("heart")
    X, y, feature_names = imodels.get_clean_dataset(
        **imodels.util.data_util.DSET_KWARGS["california_housing"]
    )

    print("shapes", X.shape, y.shape, "nunique", np.unique(y).size)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, random_state=42, test_size=0.2
    )

    coefs = []
    alphas = (0.1, 1, 10, 100, 1000, 10000)  # (0.1, 1, 10, 100, 1000, 10000)
    # alphas = 10000
    for m in [
        MarginalShrinkageLinearModelRegressor(random_state=42, alphas=alphas),
        MarginalShrinkageLinearModelRegressor(
            random_state=42, alphas=alphas, est_marginal_name=None
        ),
        MarginalShrinkageLinearModelRegressor(
            random_state=42, est_main_name=None, alphas=alphas
        ),
        MarginalShrinkageLinearModelRegressor(
            random_state=42,
            est_marginal_name="ridge",
            est_main_name="NNLS",
            marginal_sign_constraint=True,
            alphas=alphas,
        )
        # RidgeCV(alphas=alphas, fit_intercept=False),
    ]:
        print(m)
        m.fit(X_train, y_train)

        # check roc auc score
        if isinstance(m, ClassifierMixin):
            y_pred = m.predict_proba(X_test)[:, 1]
            print(
                "\ttrain roc:",
                roc_auc_score(y_train, m.predict_proba(X_train)[:, 1]).round(3),
            )
            print("\t*test roc:", roc_auc_score(y_test, y_pred).round(3))
            print(
                "\taccs",
                accuracy_score(y_train, m.predict(X_train)).round(3),
                accuracy_score(y_test, m.predict(X_test)).round(3),
                "\timb",
                np.mean(y_train).round(3),
                np.mean(y_test).round(3),
            )
        else:
            y_pred = m.predict(X_test)
            print(
                "\ttrain mse:",
                np.mean((y_train - m.predict(X_train)) ** 2).round(3),
            )
            print("\t*test mse:", np.mean((y_test - y_pred) ** 2).round(3))
            print("\ttrain r2 :", m.score(X_train, y_train).round(3))
            print(
                "\t*test r2 :",
                m.score(X_test, y_test).round(3),
            )

        if isinstance(m, MarginalShrinkageLinearModelRegressor):
            lin = m.est_main_
        else:
            lin = m

        coefs.append(deepcopy(lin.coef_))
        print("alpha best", lin.alpha_)

    diffs = pd.DataFrame({str(i): coefs[i] for i in range(len(coefs))})
    diffs["diff 0 - 1"] = diffs["0"] - diffs["1"]
    diffs["diff 1 - 2"] = diffs["1"] - diffs["2"]
    print(diffs)